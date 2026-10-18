"""A committed story Task change to done or cancelled wakes the story-completion sweep."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from shared.contracts.queues.scheduler_wake import (
    SCHEDULER_WAKE_CHANNEL,
    SchedulerWakeTarget,
    parse_scheduler_wake_hint,
)
from src.database import get_async_session
from src.dependencies import get_redis_client
from src.main import app

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

    async def rollback(self) -> None:
        self.journal.append(("rollback",))

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


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def journal() -> list:
    return []


@pytest.fixture
def redis(journal) -> NativeRedis:
    native = NativeRedis(journal)
    app.dependency_overrides[get_redis_client] = lambda: SimpleNamespace(redis=native)
    return native


def use_session(session: Session) -> Session:
    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    return session


async def call(method: str, path: str, **kwargs):
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers=INTERNAL_HEADERS,
    ) as client:
        return await client.request(method, path, **kwargs)


def transition(to_status: str):
    return call("POST", f"/api/tasks/{TASK_ID}/transition", params={"to_status": to_status})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "request_", "events"),
    [
        ("in_dev", lambda: call("POST", f"/api/tasks/{TASK_ID}/complete"), 3),
        ("testing", lambda: transition("done"), 1),
        ("in_ci", lambda: transition("cancelled"), 1),
        ("todo", lambda: call("DELETE", f"/api/tasks/{TASK_ID}"), 1),
    ],
    ids=["complete", "transition-done", "transition-cancelled", "delete"],
)
async def test_a_story_task_reaching_a_terminal_status_wakes_after_its_commit(
    journal, redis, status, request_, events
):
    session = use_session(Session(journal, make_task(status=status)))

    response = await request_()

    assert response.status_code == 200, response.text  # noqa: PLR2004
    terminal = response.json()["status"]
    assert terminal in {"done", "cancelled"}
    assert journal == [("commit", terminal), WAKE, ("refresh",)]
    assert len(session.added) == events


@pytest.mark.asyncio
async def test_an_install_cancellation_wakes_after_its_commit(journal, redis):
    use_session(Session(journal, make_task(status="todo", type="install")))

    response = await transition("cancelled")

    assert response.status_code == 200, response.text  # noqa: PLR2004
    assert response.json()["status"] == "cancelled"
    assert journal == [("commit", "cancelled"), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_an_already_cancelled_install_publishes_nothing(journal, redis):
    use_session(Session(journal, make_task(status="cancelled", type="install")))

    response = await transition("cancelled")

    assert response.status_code == 200, response.text  # noqa: PLR2004
    assert WAKE not in journal


@pytest.mark.asyncio
async def test_an_already_cancelled_task_delete_publishes_nothing(journal, redis):
    use_session(Session(journal, make_task(status="cancelled")))

    response = await call("DELETE", f"/api/tasks/{TASK_ID}")

    assert response.status_code == 200, response.text  # noqa: PLR2004
    assert journal == []


@pytest.mark.asyncio
async def test_a_held_commit_holds_the_wake(journal, redis):
    session = use_session(Session(journal, make_task(status="testing")))
    session.hold = asyncio.Event()

    request = asyncio.create_task(call("POST", f"/api/tasks/{TASK_ID}/complete"))
    await asyncio.wait_for(session.held.wait(), 1)
    assert journal == []

    session.hold.set()
    response = await request

    assert response.status_code == 200  # noqa: PLR2004
    assert journal == [("commit", "done"), WAKE, ("refresh",)]


@pytest.mark.asyncio
async def test_a_failed_commit_publishes_nothing(journal, redis):
    session = use_session(Session(journal, make_task(status="testing")))
    session.failure = OperationalError("COMMIT", {}, Exception("connection lost"))

    response = await call("POST", f"/api/tasks/{TASK_ID}/complete")

    assert response.status_code == 500  # noqa: PLR2004
    assert WAKE not in journal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "story_status", "request_", "code"),
    [
        ("backlog", "in_progress", lambda: call("POST", f"/api/tasks/{TASK_ID}/complete"), 422),
        ("in_dev", "in_progress", lambda: transition("done"), 422),
        ("testing", "waiting_human_review", lambda: transition("done"), 409),
        ("done", "in_progress", lambda: transition("cancelled"), 422),
    ],
    ids=["complete-invalid", "transition-invalid", "admission-fenced", "cancel-invalid"],
)
async def test_a_refused_operation_neither_commits_nor_wakes(
    journal, redis, status, story_status, request_, code
):
    use_session(Session(journal, make_task(status=status), story_status=story_status))

    response = await request_()

    assert response.status_code == code, response.text
    assert journal == []


@pytest.mark.asyncio
async def test_an_intermediate_hop_commits_without_waking(journal, redis):
    use_session(Session(journal, make_task(status="in_dev")))

    response = await transition("in_ci")

    assert response.status_code == 200  # noqa: PLR2004
    assert journal == [("commit", "in_ci"), ("refresh",)]


@pytest.mark.asyncio
async def test_a_storyless_task_completes_without_waking(journal, redis):
    use_session(Session(journal, make_task(status="testing", story_id=None)))

    response = await call("POST", f"/api/tasks/{TASK_ID}/complete")

    assert response.status_code == 200  # noqa: PLR2004
    assert journal == [("commit", "done"), ("refresh",)]


@pytest.mark.asyncio
async def test_a_hint_without_receivers_is_still_a_success(journal, redis):
    use_session(Session(journal, make_task(status="testing")))
    redis.receivers = 0

    response = await transition("done")

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["status"] == "done"
    assert WAKE in journal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [RedisConnectionError("redis://:secret@redis:6379"), OSError("socket closed")]
)
async def test_a_failed_publish_loses_only_the_hint(journal, redis, failure):
    session = use_session(Session(journal, make_task(status="testing")))
    redis.failure = failure

    with capture_logs() as logs:
        response = await call("POST", f"/api/tasks/{TASK_ID}/complete")

    assert response.status_code == 200  # noqa: PLR2004
    assert response.json()["status"] == "done"
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
async def test_cancellation_during_publish_propagates(journal, redis):
    use_session(Session(journal, make_task(status="testing")))
    redis.hold = asyncio.Event()

    request = asyncio.create_task(call("POST", f"/api/tasks/{TASK_ID}/complete"))
    await asyncio.wait_for(redis.held.wait(), 1)

    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert journal == [("commit", "done")]
