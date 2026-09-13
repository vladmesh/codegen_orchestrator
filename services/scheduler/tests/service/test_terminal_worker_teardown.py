import os
from types import SimpleNamespace

import pytest

from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.worker import DeleteWorkerCommand, WorkerOwnership
from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from shared.redis.client import RedisStreamClient
from src.tasks.terminal_worker_reconciliation import reconcile_terminal_story_workers


class _StoryAPI:
    def __init__(self, status: StoryStatus):
        self.status = status

    async def get_stories_by_status(self, status):
        return [SimpleNamespace(id="terminal-story")] if status == self.status else []


class _InProcessWorkerManager:
    """Consume typed teardown commands at the real Redis state boundary."""

    def __init__(self, redis):
        self.redis = redis
        self.deleted: list[str] = []

    async def publish(self, stream, payload):
        assert stream == WORKER_COMMANDS
        command = DeleteWorkerCommand.model_validate(payload)
        worker_id = command.worker_id
        meta = await self.redis.hgetall(f"worker:meta:{worker_id}")
        project_id = meta.get("project_id")
        if project_id:
            lock_key = f"workspace:lock:{project_id}"
            if await self.redis.get(lock_key) == worker_id:
                await self.redis.delete(lock_key)
        await self.redis.delete(
            f"worker:status:{worker_id}",
            f"worker:error:{worker_id}",
            f"worker:meta:{worker_id}",
        )
        self.deleted.append(worker_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status", [StoryStatus.COMPLETED, StoryStatus.FAILED, StoryStatus.ARCHIVED]
)
async def test_one_terminal_reconciliation_window_tears_down_every_story_worker(terminal_status):
    redis_client = RedisStreamClient(os.environ["REDIS_URL"])
    await redis_client.connect()
    redis = redis_client.redis
    terminal = WorkerOwnership(
        story_id="terminal-story",
        project_id="project-1",
        run_id="run-1",
        attempt_id="attempt-1",
    )
    other = terminal.model_copy(
        update={"story_id": "live-story", "run_id": "run-2", "attempt_id": "attempt-2"}
    )
    for worker_id, owner in (
        ("developer", terminal),
        ("qa-fix", terminal),
        ("other-story-worker", other),
    ):
        await redis.hset(f"worker:meta:{worker_id}", mapping=owner.as_redis_meta())
        await redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})
    await redis.hset(STORY_WORKERS_KEY, "terminal-story", "developer")
    await redis.set("workspace:lock:project-1", "developer")
    client = _InProcessWorkerManager(redis)
    try:
        assert await reconcile_terminal_story_workers(_StoryAPI(terminal_status), client) == 2

        assert client.deleted == ["developer", "qa-fix"]
        assert not await redis.exists("worker:status:developer", "worker:status:qa-fix")
        assert not await redis.exists("worker:meta:developer", "worker:meta:qa-fix")
        assert await redis.get("workspace:lock:project-1") is None
        assert await redis.hget("worker:status:other-story-worker", "status") == "RUNNING"
        assert await redis.hget("worker:meta:other-story-worker", "story_id") == "live-story"

        # The observation pass clears the legacy binding; further passes do nothing.
        assert await reconcile_terminal_story_workers(_StoryAPI(terminal_status), client) == 0
        assert await redis.hget(STORY_WORKERS_KEY, "terminal-story") is None
        assert await reconcile_terminal_story_workers(_StoryAPI(terminal_status), client) == 0
        assert client.deleted == ["developer", "qa-fix"]
    finally:
        await redis.delete(
            STORY_WORKERS_KEY,
            "workspace:lock:project-1",
            "worker:status:developer",
            "worker:status:qa-fix",
            "worker:status:other-story-worker",
            "worker:error:developer",
            "worker:error:qa-fix",
            "worker:error:other-story-worker",
            "worker:meta:developer",
            "worker:meta:qa-fix",
            "worker:meta:other-story-worker",
        )
        await redis_client.close()
