"""A committed story Task change to done or cancelled wakes the story-completion sweep."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

from fastapi import HTTPException
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from shared.contracts.queues.scheduler_wake import (
    SCHEDULER_WAKE_CHANNEL,
    SchedulerWakeTarget,
    parse_scheduler_wake_hint,
)
from src.routers._task_actions import complete_task, transition_task
from src.routers.tasks import cancel_task

STORY_ID = "story-wake"
TASK_ID = "task-wake"
WAKE = ("wake", SchedulerWakeTarget.STORY_COMPLETION)


def make_task(*, status: str, story_id: str | None = STORY_ID, type: str = "feature"):
    now = datetime.now(UTC)
    task = MagicMock()
    fields = {
        "id": TASK_ID,
        "project_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "type": type,
        "title": "Wake",
        "description": None,
        "plan": None,
        "status": status,
        "priority": 0,
        "acceptance_criteria": None,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "source_brainstorm_id": None,
        "repository_id": None,
        "story_id": story_id,
        "blocked_by_task_id": None,
        "need_e2e": False,
        "failure_metadata": None,
        "dispatch_admitted": True,
        "planning_attempt_id": None,
        "install": None,
        "install_operation": None,
        "created_at": now,
        "updated_at": now,
    }
    for name, value in fields.items():
        setattr(task, name, value)
    return task


class Session:
    """The request's transaction boundary, journalling each commit that returns."""

    def __init__(self, journal: list, task, story_status: str = "in_progress") -> None:
        self.journal = journal
        self.task = task
        self.story = SimpleNamespace(id=STORY_ID, status=story_status, engineering_stop=None)
        self.added: list = []
        self.hold: asyncio.Event | None = None
        self.held = asyncio.Event()
        self.failure: Exception | None = None

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.task)

    async def scalar(self, statement):
        return self.story

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        if self.hold is not None:
            self.held.set()
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        self.journal.append(("commit", self.task.status))

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


def complete(session, redis):
    return complete_task(TASK_ID, db=session, redis=redis)


def transition(to_status: str):
    return lambda session, redis: transition_task(
        TASK_ID, to_status=to_status, db=session, redis=redis
    )


def delete(session, redis):
    return cancel_task(TASK_ID, db=session, redis=redis)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "route", "events"),
    [
        ("in_dev", complete, 3),
        ("testing", transition("done"), 1),
        ("in_ci", transition("cancelled"), 1),
        ("todo", delete, 1),
    ],
    ids=["complete", "transition-done", "transition-cancelled", "delete"],
)
async def test_a_story_task_reaching_a_terminal_status_wakes_after_its_commit(
    journal, redis, status, route, events
):
    session = Session(journal, make_task(status=status))

    read = await route(session, redis)

    assert read.status in {"done", "cancelled"}
    assert journal == [("commit", read.status), WAKE, ("refresh",)]
    assert len(session.added) == events


@pytest.mark.asyncio
async def test_an_install_cancellation_wakes_after_its_commit(journal, redis):
    session = Session(journal, make_task(status="todo", type="install"))

    read = await transition("cancelled")(session, redis)

    assert read.status == "cancelled"
    assert journal == [("commit", "cancelled"), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_an_already_cancelled_install_publishes_nothing(journal, redis):
    session = Session(journal, make_task(status="cancelled", type="install"))

    read = await transition("cancelled")(session, redis)

    assert read.status == "cancelled"
    assert WAKE not in journal


@pytest.mark.asyncio
async def test_an_already_cancelled_task_delete_publishes_nothing(journal, redis):
    session = Session(journal, make_task(status="cancelled"))

    read = await delete(session, redis)

    assert read.status == "cancelled"
    assert journal == []


@pytest.mark.asyncio
async def test_a_held_commit_holds_the_wake(journal, redis):
    session = Session(journal, make_task(status="testing"))
    session.hold = asyncio.Event()

    request = asyncio.create_task(complete(session, redis))
    await asyncio.wait_for(session.held.wait(), 1)
    assert journal == []

    session.hold.set()
    read = await request

    assert read.status == "done"
    assert journal == [("commit", "done"), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_a_failed_commit_publishes_nothing(journal, redis):
    session = Session(journal, make_task(status="testing"))
    session.failure = OperationalError("COMMIT", {}, Exception("connection lost"))

    with pytest.raises(OperationalError):
        await complete(session, redis)

    assert journal == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "story_status", "route", "code"),
    [
        ("backlog", "in_progress", complete, 422),
        ("in_dev", "in_progress", transition("done"), 422),
        ("testing", "waiting_human_review", transition("done"), 409),
        ("done", "in_progress", transition("cancelled"), 422),
    ],
    ids=["complete-invalid", "transition-invalid", "admission-fenced", "cancel-invalid"],
)
async def test_a_refused_operation_neither_commits_nor_wakes(
    journal, redis, status, story_status, route, code
):
    session = Session(journal, make_task(status=status), story_status=story_status)

    with pytest.raises(HTTPException) as refused:
        await route(session, redis)

    assert refused.value.status_code == code
    assert journal == []


@pytest.mark.asyncio
async def test_an_intermediate_hop_commits_without_waking(journal, redis):
    session = Session(journal, make_task(status="in_dev"))

    read = await transition("in_ci")(session, redis)

    assert read.status == "in_ci"
    assert journal == [("commit", "in_ci"), ("refresh",)]


@pytest.mark.asyncio
async def test_a_storyless_task_completes_without_waking(journal, redis):
    session = Session(journal, make_task(status="testing", story_id=None))

    read = await complete(session, redis)

    assert read.status == "done"
    assert journal == [("commit", "done"), ("refresh",)]


@pytest.mark.asyncio
async def test_a_hint_without_receivers_is_still_a_success(journal, redis, native):
    session = Session(journal, make_task(status="testing"))
    native.receivers = 0

    read = await transition("done")(session, redis)

    assert read.status == "done"
    assert journal == [("commit", "done"), WAKE, ("refresh",)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_a_failed_publish_loses_only_the_hint(journal, redis, native, failure):
    session = Session(journal, make_task(status="testing"))
    native.failure = failure

    with capture_logs() as logs:
        read = await complete(session, redis)

    assert read.status == "done"
    assert journal == [("commit", "done"), ("refresh",)]
    assert len(session.added) == 1
    [failed] = [log for log in logs if log["event"] == "story_completion_wake_failed"]
    assert failed == {
        "event": "story_completion_wake_failed",
        "log_level": "warning",
        "task_id": TASK_ID,
        "story_id": STORY_ID,
        "error_type": type(failure).__name__,
    }


@pytest.mark.asyncio
async def test_cancellation_during_publish_propagates(journal, redis, native):
    session = Session(journal, make_task(status="testing"))
    native.hold = asyncio.Event()

    request = asyncio.create_task(complete(session, redis))
    await asyncio.wait_for(native.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == [("commit", "done")]
