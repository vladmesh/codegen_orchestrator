"""A committed Story entry into PR review wakes the PR/CI sweep."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs
from wake_doubles import NativeRedis

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.models import Story
from src.routers.stories import deploy_story, pr_review_story

STORY_ID = "story-wake"
PR_NUMBER = 42
WAKE = ("wake", SchedulerWakeTarget.PR_CI)


def make_story(status: str) -> Story:
    now = datetime.now(UTC)
    return Story(
        id=STORY_ID,
        project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        title="Wake",
        type="product",
        status=status,
        waiting_on="none",
        priority=0,
        created_by="po",
        unverified_decisions=[],
        pr_number=PR_NUMBER,
        created_at=now,
        updated_at=now,
    )


class Session:
    """Answers the Story lock and journals each commit and refresh that returns."""

    def __init__(self, journal: list, status: str = "in_progress") -> None:
        self.journal = journal
        self.story = make_story(status)
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: Exception | None = None

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.story)

    async def commit(self) -> None:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        self.journal.append(("commit", self.story.status))

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


def review(session: Session, redis):
    return pr_review_story(STORY_ID, None, db=session, redis=redis)


@pytest.mark.asyncio
async def test_an_entry_publishes_one_hint_after_its_commit_and_before_the_refresh(journal, redis):
    read = await review(Session(journal), redis)

    assert read.status == "pr_review"
    assert read.waiting_on == "ci"
    assert read.pr_number == PR_NUMBER
    assert journal == [("commit", "pr_review"), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_each_later_work_cycle_entry_publishes_again(journal, redis):
    session = Session(journal)
    await review(session, redis)
    # A CI-failure fix cycle returns the story to work before its next PR review.
    session.story.status = "in_progress"

    await review(session, redis)

    assert journal == [("commit", "pr_review"), WAKE, ("refresh",)] * 2


@pytest.mark.asyncio
async def test_nothing_is_published_while_the_commit_is_held(journal, redis):
    session = Session(journal)
    session.hold = asyncio.Event()

    request = asyncio.create_task(review(session, redis))
    await asyncio.wait_for(session.held.wait(), 1)
    await asyncio.sleep(0)
    assert journal == []

    session.hold.set()
    await request
    assert journal == [("commit", "pr_review"), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_a_failed_commit_publishes_nothing(journal, redis):
    session = Session(journal)
    session.failure = OperationalError("COMMIT", {}, Exception("connection lost"))

    with pytest.raises(OperationalError):
        await review(session, redis)

    assert journal == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pr_review", "deploying", "completed", "created"])
async def test_a_refused_entry_publishes_nothing(journal, redis, status):
    session = Session(journal, status)

    with pytest.raises(HTTPException) as refused:
        await review(session, redis)

    assert refused.value.status_code == 422
    assert session.story.status == status
    assert journal == []


@pytest.mark.asyncio
async def test_another_transition_publishes_nothing(journal, redis):
    session = Session(journal, "pr_review")

    read = await deploy_story(STORY_ID, None, db=session)

    assert read.status == "deploying"
    assert journal == [("commit", "deploying"), ("refresh",)]


@pytest.mark.asyncio
async def test_zero_subscribers_is_a_published_hint(journal, redis, native):
    native.receivers = 0

    with capture_logs() as logs:
        read = await review(Session(journal), redis)

    assert read.status == "pr_review"
    assert journal == [("commit", "pr_review"), WAKE, ("refresh",)]
    [published] = [log for log in logs if log["event"] == "pr_ci_wake_published"]
    assert published["story_id"] == STORY_ID
    assert published["receivers"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_a_failed_publish_loses_only_the_hint(journal, redis, native, failure):
    native.failure = failure

    with capture_logs() as logs:
        read = await review(Session(journal), redis)

    assert read.status == "pr_review"
    assert journal == [("commit", "pr_review"), ("refresh",)]
    [failed] = [log for log in logs if log["event"] == "pr_ci_wake_failed"]
    assert failed == {
        "event": "pr_ci_wake_failed",
        "log_level": "warning",
        "story_id": STORY_ID,
        "error_type": type(failure).__name__,
    }


@pytest.mark.asyncio
async def test_cancellation_during_publish_propagates_after_the_commit(journal, redis, native):
    native.hold = asyncio.Event()

    request = asyncio.create_task(review(Session(journal), redis))
    await asyncio.wait_for(native.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == [("commit", "pr_review")]
