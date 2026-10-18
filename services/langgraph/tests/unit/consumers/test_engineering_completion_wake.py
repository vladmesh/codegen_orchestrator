"""A successful story engineering settlement wakes the story-completion sweep after its writes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from shared.contracts.queues.scheduler_wake import (
    SCHEDULER_WAKE_CHANNEL,
    SchedulerWakeTarget,
    parse_scheduler_wake_hint,
)
from shared.queues import DEPLOY_QUEUE
from src.consumers import engineering_result_handler as handler
from tests.unit.factories import make_project

COMMIT = "c0ffee0000000000000000000000000000000000"
RUN_ID = "eng-wake-1"
TASK_ID = "task-wake-1"
STORY_ID = "story-wake-1"
TASK_WRITES = [
    ("post", f"tasks/{TASK_ID}/transition", "in_ci"),
    ("post", f"tasks/{TASK_ID}/transition", "testing"),
    ("post", f"tasks/{TASK_ID}/transition", "done"),
    ("post", f"tasks/{TASK_ID}/events", "iteration_end"),
]
RUN_COMPLETED = ("patch", f"runs/{RUN_ID}", "completed")
WAKE = ("wake", SchedulerWakeTarget.STORY_COMPLETION)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class Api:
    """The API boundary the handler writes through, journalling each returned write."""

    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.holds: dict[tuple, asyncio.Event] = {}
        self.failures: dict[tuple, Exception] = {}

    async def _write(self, entry: tuple) -> None:
        if entry in self.holds:
            await self.holds[entry].wait()
        if entry in self.failures:
            raise self.failures[entry]
        self.journal.append(entry)

    async def patch(self, path: str, json: dict) -> None:
        await self._write(("patch", path, json.get("status")))

    async def post(self, path: str, params: dict | None = None, json: dict | None = None) -> None:
        if path.endswith("/transition"):
            await self._write(("post", path, params["to_status"]))
        elif path.endswith("/events"):
            await self._write(("post", path, json["event_type"]))
        else:
            await self._write(("post", path, json["type"]))

    async def transition_story(self, story_id: str, action: str) -> None:
        await self._write(("story", story_id, action))

    async def get_run(self, run_id: str) -> SimpleNamespace:
        return SimpleNamespace(run_metadata={}, story_id=None)

    async def get_primary_repository(self, project_id: str) -> None:
        return None


class NativeRedis:
    """The connected client's native Pub/Sub and key reads."""

    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.receivers = 1
        self.failure: Exception | None = None
        self.hold: asyncio.Event | None = None

    async def publish(self, channel: str, message: str) -> int:
        if self.hold is not None:
            await self.hold.wait()
        if self.failure is not None:
            raise self.failure
        assert channel == SCHEDULER_WAKE_CHANNEL
        self.journal.append(("wake", parse_scheduler_wake_hint(message).target))
        return self.receivers

    async def get(self, key: str) -> None:
        return None

    async def hgetall(self, key: str) -> dict:
        return {}


class StreamClient:
    def __init__(self, journal: list) -> None:
        self.journal = journal
        self.redis = NativeRedis(journal)

    async def publish_flat(self, stream: str, fields: dict) -> None:
        self.journal.append(("callback", fields["event"]))

    async def publish_message(self, stream: str, message: object) -> None:
        self.journal.append(("stream", stream))


@pytest.fixture
def journal() -> list:
    return []


@pytest.fixture
def api(journal, monkeypatch) -> Api:
    fake = Api(journal)
    monkeypatch.setattr(handler, "api_client", fake)
    return fake


@pytest.fixture
def redis(journal) -> StreamClient:
    return StreamClient(journal)


def success(redis, *, story_id=STORY_ID, planning_task_id=TASK_ID, skip_deploy=False, **result):
    return handler.handle_engineering_success(
        handler.EngineeringSuccessParams(
            result={"engineering_status": "done", "commit_sha": COMMIT, **result},
            task_id=RUN_ID,
            project=make_project(name="wake", config={}),
            callback_stream="po:response:wake",
            redis=redis,
            skip_deploy=skip_deploy,
            telegram_chat_id="u-1",
            planning_task_id=planning_task_id,
            story_id=story_id,
        )
    )


@pytest.mark.asyncio
async def test_a_planned_story_success_wakes_after_run_task_and_event_writes(api, redis, journal):
    out = await success(redis)

    assert out["status"] == "success"
    assert journal == [RUN_COMPLETED, *TASK_WRITES, WAKE, ("callback", "completed")]


@pytest.mark.asyncio
async def test_a_taskless_deploy_fix_success_wakes_before_its_direct_deploy(api, redis, journal):
    out = await success(redis, planning_task_id=None)

    deploy_run = ("post", "runs/", "deploy")
    assert out["deploy_task_id"] == "deploy-wake-1"
    assert journal == [
        RUN_COMPLETED,
        WAKE,
        ("callback", "progress"),
        deploy_run,
        ("stream", DEPLOY_QUEUE),
    ]


@pytest.mark.asyncio
async def test_a_storyless_success_settles_without_a_wake(api, redis, journal):
    out = await success(redis, story_id=None, planning_task_id=None)

    assert out["status"] == "success"
    assert WAKE not in journal
    assert journal[0] == RUN_COMPLETED
    assert ("stream", DEPLOY_QUEUE) in journal


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [RUN_COMPLETED, *TASK_WRITES])
async def test_a_held_settlement_write_holds_the_wake(api, redis, journal, held):
    api.holds[held] = asyncio.Event()
    settlement = asyncio.create_task(success(redis))
    await settle()

    assert not settlement.done()
    assert WAKE not in journal

    api.holds[held].set()
    await settlement
    assert journal.index(WAKE) == journal.index(TASK_WRITES[-1]) + 1


@pytest.mark.asyncio
async def test_a_failed_terminal_run_write_publishes_nothing(api, redis, journal):
    api.failures[RUN_COMPLETED] = RuntimeError("api unavailable")

    with pytest.raises(RuntimeError):
        await success(redis)

    assert journal == []


@pytest.mark.asyncio
async def test_failed_task_and_event_writes_stay_best_effort_and_still_wake(api, redis, journal):
    api.failures[TASK_WRITES[1]] = RuntimeError("transition refused")
    api.failures[TASK_WRITES[3]] = RuntimeError("event refused")

    out = await success(redis)

    assert out["status"] == "success"
    assert journal == [RUN_COMPLETED, TASK_WRITES[0], WAKE, ("callback", "completed")]


@pytest.mark.asyncio
async def test_zero_receivers_is_a_valid_wake(api, redis, journal):
    redis.redis.receivers = 0

    out = await success(redis)

    assert out["status"] == "success"
    assert journal == [RUN_COMPLETED, *TASK_WRITES, WAKE, ("callback", "completed")]


@pytest.mark.asyncio
async def test_a_failed_publication_loses_only_the_hint(api, redis, journal):
    redis.redis.failure = RedisConnectionError("redis://:secret@redis:6379 unreachable")

    with patch.object(handler, "logger") as logger:
        out = await success(redis, planning_task_id=None)

    assert out["status"] == "success"
    assert out["deploy_task_id"] == "deploy-wake-1"
    assert journal == [
        RUN_COMPLETED,
        ("callback", "progress"),
        ("post", "runs/", "deploy"),
        ("stream", DEPLOY_QUEUE),
    ]
    [failed] = [
        c for c in logger.warning.call_args_list if c.args == ("story_completion_wake_failed",)
    ]
    assert failed.kwargs == {"story_id": STORY_ID, "error_type": "ConnectionError"}


@pytest.mark.asyncio
async def test_cancellation_during_the_wake_propagates_without_later_effects(api, redis, journal):
    redis.redis.hold = asyncio.Event()
    settlement = asyncio.create_task(success(redis))
    await settle()

    settlement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await settlement

    assert journal == [RUN_COMPLETED, *TASK_WRITES]


@pytest.mark.asyncio
async def test_a_success_without_a_commit_fails_without_a_wake(api, redis, journal):
    out = await success(redis, commit_sha=None)

    assert out["status"] == "failed"
    assert WAKE not in journal
    assert ("patch", f"runs/{RUN_ID}", "failed") in journal


@pytest.mark.asyncio
async def test_an_uncomputable_derived_key_fails_without_a_wake(api, redis, journal, monkeypatch):
    monkeypatch.setattr(
        handler, "_uncomputable_derived_keys_at", AsyncMock(return_value=["UNSUPPORTED_URL"])
    )

    out = await success(redis)

    assert out["status"] == "failed"
    assert WAKE not in journal
    assert ("patch", f"runs/{RUN_ID}", "failed") in journal


@pytest.mark.asyncio
async def test_a_failed_attempt_settles_without_a_wake(api, redis, journal):
    out = await handler.fail_job(
        RUN_ID, "agent failed", TASK_ID, redis=redis, story_id=STORY_ID, project_id="p-1"
    )

    assert out["status"] == "failed"
    assert journal == [("patch", f"runs/{RUN_ID}", "failed"), ("post", TASK_WRITES[0][1], "failed")]


@pytest.mark.asyncio
async def test_a_gave_up_attempt_settles_without_a_wake(api, redis, journal, monkeypatch):
    monkeypatch.setattr(handler, "notify_admins_best_effort", AsyncMock())

    out = await handler.handle_worker_gave_up(
        task_id=RUN_ID,
        project_id="p-1",
        planning_task_id=None,
        story_id=STORY_ID,
        reason="cannot do it",
        telegram_chat_id="",
        redis=redis,
    )

    assert out["status"] == "gave_up"
    assert journal == [("patch", f"runs/{RUN_ID}", "failed"), ("story", STORY_ID, "human-review")]
