"""The first committed typed success of an ordinary story deploy wakes lifecycle supervision."""

import asyncio
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs
from wake_doubles import NativeRedis

from shared.contracts.dto.run import RunStatus, RunType, RunUpdate
from shared.contracts.dto.run_result import DeployRunResult
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.models import Run
from src.routers.runs import update_run

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = "deploy-wake"
STORY_ID = "story-wake"
WAKE = ("wake", SchedulerWakeTarget.LIFECYCLE_SUPERVISION)
SUCCESS = DeployRunResult(
    deploy_outcome=DeployOutcome.SUCCESS, deployed_url="https://wake.example"
).model_dump(mode="json")
CODE_FIX = DeployRunResult(
    deploy_outcome=DeployOutcome.CODE_FIX, error_details="build failed"
).model_dump(mode="json")
COMMITTED = [("commit",), ("refresh",)]
WOKEN = [("commit",), WAKE, ("refresh",)]


def make_run(
    *,
    status: str = RunStatus.RUNNING.value,
    result: dict | None = None,
    story_id: str | None = STORY_ID,
    type: str = RunType.DEPLOY.value,
    metadata: dict | None = None,
) -> Run:
    return Run(
        id=RUN_ID,
        type=type,
        status=status,
        project_id=PROJECT_ID,
        story_id=story_id,
        task_id=None,
        run_metadata={} if metadata is None else metadata,
        result=result,
        completed_at=None,
    )


class Session:
    """Answers the Run lock and journals each commit and refresh that returns."""

    def __init__(self, journal: list, run: Run) -> None:
        self.journal = journal
        self.run = run
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: Exception | None = None

    async def execute(self, statement):
        run = self.run
        edges = SimpleNamespace(
            type=run.type, story_id=run.story_id, task_id=run.task_id, project_id=run.project_id
        )
        return SimpleNamespace(one_or_none=lambda: edges, scalar_one_or_none=lambda: run)

    async def scalar(self, statement):
        # A QA Run without a budget reservation, as one admitted before reservations.
        return None

    async def commit(self) -> None:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        self.journal.append(("commit",))

    async def refresh(self, row) -> None:
        self.journal.append(("refresh",))


@pytest.fixture
def journal() -> list:
    return []


@pytest.fixture
def native(journal) -> NativeRedis:
    return NativeRedis(journal)


@pytest.fixture
def redis(native) -> SimpleNamespace:
    return SimpleNamespace(redis=native)


def patch(session: Session, redis, *, internal: bool = True, **fields):
    return update_run(
        RUN_ID,
        RunUpdate(**fields),
        db=session,
        x_telegram_id=None,
        _is_internal=internal,
        credentials=None,
        redis=redis,
    )


def succeed(session: Session, redis):
    return patch(session, redis, status=RunStatus.COMPLETED, result=SUCCESS)


@pytest.mark.asyncio
async def test_a_first_deploy_success_publishes_one_hint_after_its_commit_and_before_the_refresh(
    journal, redis
):
    run = await succeed(Session(journal, make_run()), redis)

    assert run.status == RunStatus.COMPLETED
    assert journal == WOKEN


@pytest.mark.asyncio
async def test_nothing_is_published_while_the_commit_is_held(journal, redis):
    session = Session(journal, make_run())
    session.hold = asyncio.Event()

    request = asyncio.create_task(succeed(session, redis))
    await asyncio.wait_for(session.held.wait(), 1)
    await asyncio.sleep(0)
    assert journal == []

    session.hold.set()
    await request
    assert journal == WOKEN


@pytest.mark.asyncio
async def test_a_failed_commit_publishes_nothing(journal, redis):
    session = Session(journal, make_run())
    session.failure = OperationalError("COMMIT", {}, Exception("connection lost"))

    with pytest.raises(OperationalError):
        await succeed(session, redis)

    assert journal == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"result": SUCCESS}, {"status": RunStatus.COMPLETED}),
        ({"status": RunStatus.COMPLETED}, {"result": SUCCESS}),
    ],
    ids=["result_then_status", "status_then_result"],
)
async def test_a_split_write_publishes_when_its_second_half_completes_the_proof(
    journal, redis, first, second
):
    session = Session(journal, make_run())

    await patch(session, redis, **first)
    assert journal == COMMITTED

    journal.clear()
    await patch(session, redis, **second)
    assert journal == WOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, RunStatus.COMPLETED])
async def test_a_late_success_of_a_cancelled_deploy_publishes_its_hint(journal, redis, status):
    session = Session(journal, make_run(status=RunStatus.CANCELLED.value))
    fields = {"result": SUCCESS} if status is None else {"result": SUCCESS, "status": status}

    run = await patch(session, redis, **fields)

    assert run.result == SUCCESS
    assert journal == WOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"status": RunStatus.COMPLETED, "result": SUCCESS},
        {"run_metadata": {"deployed_by": "deployer"}},
    ],
    ids=["identical_replay", "metadata_only"],
)
async def test_a_write_to_a_proven_deploy_publishes_nothing(journal, redis, fields):
    session = Session(journal, make_run(status=RunStatus.COMPLETED.value, result=SUCCESS))

    await patch(session, redis, **fields)

    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "update"),
    [
        ({"temporary_access_grant_id": "grant-1"}, {}),
        ({"temporary_access_operation": None}, {}),
        ({}, {"run_metadata": {"temporary_access_grant_id": None}}),
        ({"temporary_access_operation": "revoke"}, {"run_metadata": None}),
        (
            {"temporary_access_grant_id": "grant-1"},
            {"run_metadata": {"temporary_access_grant_id": None}},
        ),
    ],
    ids=["stored_grant", "stored_null_operation", "added_null_grant", "cleared", "nulled"],
)
async def test_a_temporary_access_operation_success_publishes_nothing(
    journal, redis, stored, update
):
    session = Session(journal, make_run(metadata=stored))

    run = await patch(session, redis, status=RunStatus.COMPLETED, result=SUCCESS, **update)

    assert run.result == SUCCESS
    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "fields"),
    [
        (make_run(story_id=None), {"status": RunStatus.COMPLETED, "result": SUCCESS}),
        (make_run(type=RunType.QA.value), {"status": RunStatus.COMPLETED, "result": SUCCESS}),
        (make_run(), {"status": RunStatus.FAILED, "result": CODE_FIX}),
        (make_run(), {"status": RunStatus.RUNNING, "result": SUCCESS}),
        (make_run(), {"status": RunStatus.CANCELLED}),
        (make_run(), {"status": RunStatus.COMPLETED, "result": {"deploy_outcome": "deployed"}}),
    ],
    ids=["storyless", "qa_run", "code_fix", "intermediate", "no_result", "malformed_result"],
)
async def test_a_write_without_story_deploy_success_publishes_nothing(journal, redis, run, fields):
    await patch(Session(journal, run), redis, **fields)

    assert journal == COMMITTED


@pytest.mark.asyncio
async def test_a_malformed_stored_result_still_takes_an_unrelated_update(journal, redis):
    stored = {"deploy_outcome": "deployed"}
    session = Session(journal, make_run(status=RunStatus.COMPLETED.value, result=stored))

    run = await patch(session, redis, run_metadata={"deployed_by": "deployer"})

    assert run.result == stored
    assert journal == COMMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "fields", "status_code"),
    [
        (make_run(), {"internal": False, "status": RunStatus.COMPLETED, "result": SUCCESS}, 401),
        (
            make_run(status=RunStatus.FAILED.value, result=CODE_FIX),
            {"status": RunStatus.COMPLETED, "result": SUCCESS},
            409,
        ),
        (
            make_run(status=RunStatus.COMPLETED.value, result=SUCCESS),
            {"status": RunStatus.RUNNING},
            409,
        ),
    ],
    ids=["unauthenticated", "outcome_rewrite", "resurrection"],
)
async def test_a_refused_update_publishes_nothing(journal, redis, run, fields, status_code):
    with pytest.raises(HTTPException) as refused:
        await patch(Session(journal, run), redis, **fields)

    assert refused.value.status_code == status_code
    assert journal == []


@pytest.mark.asyncio
async def test_zero_subscribers_is_a_published_hint(journal, redis, native):
    native.receivers = 0

    with capture_logs() as logs:
        await succeed(Session(journal, make_run()), redis)

    assert journal == WOKEN
    [published] = [log for log in logs if log["event"] == "lifecycle_supervision_wake_published"]
    assert published["receivers"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_a_failed_publish_loses_only_the_hint(journal, redis, native, failure):
    native.failure = failure

    with capture_logs() as logs:
        run = await succeed(Session(journal, make_run()), redis)

    assert run.status == RunStatus.COMPLETED
    assert run.result == SUCCESS
    assert journal == COMMITTED
    [failed] = [log for log in logs if log["event"] == "lifecycle_supervision_wake_failed"]
    assert failed == {
        "event": "lifecycle_supervision_wake_failed",
        "log_level": "warning",
        "run_id": RUN_ID,
        "story_id": STORY_ID,
        "error_type": type(failure).__name__,
    }


@pytest.mark.asyncio
async def test_cancellation_during_publish_propagates_after_the_commit(journal, redis, native):
    native.hold = asyncio.Event()

    request = asyncio.create_task(succeed(Session(journal, make_run()), redis))
    await asyncio.wait_for(native.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == [("commit",)]
