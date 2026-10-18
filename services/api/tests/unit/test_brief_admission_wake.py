"""A committed Product Brief admission that releases Tasks wakes the task dispatcher."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from shared.contracts.dto.product_brief import (
    ProductBriefAdmissionCommand,
    ProductBriefAdmissionOutcome,
)
from shared.contracts.dto.story_planning import PLANNING_MAX_RETRIES_CONFIG_KEY
from shared.contracts.queues.scheduler_wake import (
    SCHEDULER_WAKE_CHANNEL,
    SchedulerWakeTarget,
    parse_scheduler_wake_hint,
)
from shared.models import ProductBrief, Project, RequirementCoverage, Story, SystemConfig, Task
from src.routers.product_briefs import admit_product_brief_coverage

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
BRIEF_ID = "brief-wake"
STORY_ID = "story-wake"
ATTEMPT = "plan-wake"
TASK_ID = "task-wake"
WAKE = ("wake", SchedulerWakeTarget.TASK_DISPATCHER)


def make_brief(**fields) -> ProductBrief:
    return ProductBrief(
        id=BRIEF_ID,
        story_id=STORY_ID,
        project_id=PROJECT_ID,
        confirmed_at=datetime.now(UTC),
        planning_attempt_id=ATTEMPT,
        planning_attempt_active=True,
        content={"must_requirements": [{"id": "r1"}]},
        **fields,
    )


def make_story() -> Story:
    now = datetime.now(UTC)
    return Story(
        id=STORY_ID,
        project_id=PROJECT_ID,
        title="Wake",
        type="product",
        status="in_progress",
        waiting_on="none",
        priority=0,
        created_by="po",
        unverified_decisions=[],
        created_at=now,
        updated_at=now,
    )


def planned_task(*, dispatch_admitted: bool = False) -> Task:
    return Task(
        id=TASK_ID,
        project_id=PROJECT_ID,
        story_id=STORY_ID,
        planning_attempt_id=ATTEMPT,
        status="todo",
        dispatch_admitted=dispatch_admitted,
    )


def covered_by(task_id: str | None, **fields) -> list[RequirementCoverage]:
    return [
        RequirementCoverage(
            brief_id=BRIEF_ID,
            requirement_id="r1",
            planning_attempt_id=ATTEMPT,
            task_id=task_id,
            **fields,
        )
    ]


class Session:
    """Answers the admission's reads and journals each commit and refresh that returns."""

    def __init__(self, journal: list, brief, *, tasks=(), coverage=()) -> None:
        self.journal = journal
        self.brief = brief
        self.story = make_story()
        self.rows = {
            ProductBrief: brief,
            Project: Project(id=PROJECT_ID, title="Wake", owner_id=None),
            Story: self.story,
        }
        self.listed = {Task: list(tasks), RequirementCoverage: list(coverage)}
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: Exception | None = None

    async def execute(self, statement):
        row = self.rows[statement.column_descriptions[0]["entity"]]
        return SimpleNamespace(scalar_one_or_none=lambda: row)

    async def scalars(self, statement):
        rows = self.listed[statement.column_descriptions[0]["entity"]]
        return SimpleNamespace(all=lambda: rows)

    async def get(self, model, key, **_kwargs):
        assert (model, key) == (SystemConfig, PLANNING_MAX_RETRIES_CONFIG_KEY)
        return SystemConfig(key=key, value=3, category="supervisor")

    async def commit(self) -> None:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        self.journal.append(("commit", self.brief.coverage_admitted_at is not None))

    async def refresh(self, row) -> None:
        self.journal.append(("refresh",))


class NativeRedis:
    """The initialized client's native Pub/Sub publish."""

    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.receivers = 1
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: BaseException | None = None

    async def publish(self, channel: str, message: str) -> int:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        assert channel == SCHEDULER_WAKE_CHANNEL
        self.journal.append(("wake", parse_scheduler_wake_hint(message).target))
        return self.receivers


@pytest.fixture
def journal() -> list:
    return []


@pytest.fixture
def native(journal) -> NativeRedis:
    return NativeRedis(journal)


@pytest.fixture
def redis(native) -> SimpleNamespace:
    return SimpleNamespace(redis=native)


def releasing(journal: list) -> Session:
    return Session(journal, make_brief(), tasks=[planned_task()], coverage=covered_by(TASK_ID))


def admit(session: Session, redis, attempt: str = ATTEMPT):
    return admit_product_brief_coverage(
        BRIEF_ID,
        ProductBriefAdmissionCommand(planning_attempt_id=attempt),
        x_telegram_id=None,
        db=session,
        internal=True,
        credentials=None,
        redis=redis,
    )


@pytest.mark.asyncio
async def test_a_release_publishes_one_hint_after_its_commit_and_before_the_refresh(journal, redis):
    read = await admit(releasing(journal), redis)

    assert read.outcome is ProductBriefAdmissionOutcome.ADMITTED
    assert read.released_task_ids == [TASK_ID]
    assert journal == [("commit", True), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_nothing_is_published_while_the_commit_is_held(journal, redis):
    session = releasing(journal)
    session.hold = asyncio.Event()

    request = asyncio.create_task(admit(session, redis))
    await asyncio.wait_for(session.held.wait(), 1)
    await asyncio.sleep(0)
    assert journal == []

    session.hold.set()
    await request
    assert journal == [("commit", True), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_a_failed_commit_publishes_nothing(journal, redis):
    session = releasing(journal)
    session.failure = OperationalError("COMMIT", {}, Exception("connection lost"))

    with pytest.raises(OperationalError):
        await admit(session, redis)

    assert journal == []


@pytest.mark.asyncio
async def test_a_replay_publishes_nothing(journal, redis):
    admitted_at = datetime.now(UTC)
    session = Session(journal, make_brief(coverage_admitted_at=admitted_at))

    read = await admit(session, redis)

    assert read.outcome is ProductBriefAdmissionOutcome.ALREADY_ADMITTED
    assert journal == []


@pytest.mark.asyncio
async def test_an_incomplete_admission_publishes_nothing(journal, redis):
    session = Session(journal, make_brief(), tasks=[planned_task()])

    read = await admit(session, redis)

    assert read.outcome is ProductBriefAdmissionOutcome.INCOMPLETE
    assert read.missing_requirement_ids == ["r1"]
    assert journal == []


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["superseded_attempt", "coverage_outside_the_plan"])
async def test_a_refused_admission_publishes_nothing(journal, redis, refusal):
    if refusal == "superseded_attempt":
        session, attempt = releasing(journal), "plan-old"
    else:
        session = Session(journal, make_brief(), coverage=covered_by("task-elsewhere"))
        attempt = ATTEMPT

    with pytest.raises(HTTPException) as refused:
        await admit(session, redis, attempt)

    assert refused.value.status_code == 409
    assert journal == []


@pytest.mark.asyncio
async def test_a_fully_returned_taskless_plan_commits_its_park_and_publishes_nothing(
    journal, redis
):
    session = Session(journal, make_brief(), coverage=covered_by(None, returned_reason="no API"))

    read = await admit(session, redis)

    assert read.outcome is ProductBriefAdmissionOutcome.ADMITTED
    assert read.released_task_ids == []
    assert session.story.status == "waiting_human_review"
    assert journal == [("commit", True), ("refresh",)]


@pytest.mark.asyncio
async def test_an_admission_whose_tasks_were_already_released_publishes_nothing(journal, redis):
    session = Session(
        journal,
        make_brief(),
        tasks=[planned_task(dispatch_admitted=True)],
        coverage=covered_by(TASK_ID),
    )

    read = await admit(session, redis)

    assert read.released_task_ids == []
    assert journal == [("commit", True), ("refresh",)]


@pytest.mark.asyncio
async def test_zero_subscribers_is_a_published_hint(journal, redis, native):
    native.receivers = 0

    with capture_logs() as logs:
        read = await admit(releasing(journal), redis)

    assert read.released_task_ids == [TASK_ID]
    assert journal == [("commit", True), WAKE, ("refresh",)]
    [published] = [log for log in logs if log["event"] == "task_dispatcher_wake_published"]
    assert published["receivers"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_a_failed_publish_loses_only_the_hint(journal, redis, native, failure):
    native.failure = failure

    with capture_logs() as logs:
        read = await admit(releasing(journal), redis)

    assert read.outcome is ProductBriefAdmissionOutcome.ADMITTED
    assert read.released_task_ids == [TASK_ID]
    assert journal == [("commit", True), ("refresh",)]
    [failed] = [log for log in logs if log["event"] == "task_dispatcher_wake_failed"]
    assert failed == {
        "event": "task_dispatcher_wake_failed",
        "log_level": "warning",
        "brief_id": BRIEF_ID,
        "story_id": STORY_ID,
        "error_type": type(failure).__name__,
    }


@pytest.mark.asyncio
async def test_cancellation_during_publish_propagates_after_the_commit(journal, redis, native):
    native.hold = asyncio.Event()

    request = asyncio.create_task(admit(releasing(journal), redis))
    await asyncio.wait_for(native.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == [("commit", True)]
