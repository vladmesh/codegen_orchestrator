"""Terminal reconciliation through worker-manager's canonical removal boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from scheduler_tasks.terminal_worker_reconciliation import reconcile_terminal_story_workers

from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.worker import DeleteWorkerCommand, WorkerOwnership
from shared.contracts.worker_evidence import removed_worker_evidence_key
from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from shared.redis.client import RedisStreamClient
from src.config import settings
from src.worker_removal import WorkerRemoval


class _StoryAPI:
    def __init__(self, status: StoryStatus, project_id: str):
        self.status = status
        self.project_id = project_id

    async def get_stories_by_status(self, status):
        return (
            [SimpleNamespace(id="terminal-story", project_id=self.project_id)]
            if status == self.status
            else []
        )


def _container() -> dict:
    return {
        "Image": "sha256:" + "1" * 64,
        "Config": {"Image": "worker:latest", "Env": ["WORKER_AGENT_TYPE=codex"]},
        "State": {
            "Status": "running",
            "Running": True,
            "OOMKilled": False,
            "ExitCode": 0,
            "StartedAt": "2026-09-13T12:00:00Z",
            "FinishedAt": "0001-01-01T00:00:00Z",
            "Error": "",
        },
        "Mounts": [],
    }


class _CanonicalWorkerManagerBoundary:
    """Deliver scheduler commands to the production WorkerRemoval implementation."""

    def __init__(self, redis, docker):
        self.redis = redis
        self.removal = WorkerRemoval(
            redis,
            docker,
            unregister_broker_worker=AsyncMock(),
            release_workspace_lock=self._release_workspace_lock,
        )

    async def _release_workspace_lock(self, worker_id: str, project_id: str | None):
        if project_id is None:
            return
        lock_key = f"workspace:lock:{project_id}"
        if await self.redis.get(lock_key) == worker_id:
            await self.redis.delete(lock_key)

    async def publish(self, stream, payload):
        assert stream == WORKER_COMMANDS
        command = DeleteWorkerCommand.model_validate(payload)
        await self.removal.delete_worker(command.worker_id, command.reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status", [StoryStatus.COMPLETED, StoryStatus.FAILED, StoryStatus.ARCHIVED]
)
async def test_one_terminal_window_uses_canonical_removal_for_every_story_worker(
    terminal_status,
):
    suffix = uuid.uuid4().hex[:8]
    worker_ids = {
        "developer": f"developer-{suffix}",
        "qa_fix": f"qa-fix-{suffix}",
        "other": f"other-story-{suffix}",
    }
    project_id = f"project-{suffix}"
    redis_client = RedisStreamClient()
    await redis_client.connect()
    redis = redis_client.redis
    terminal = WorkerOwnership(
        story_id="terminal-story",
        project_id=project_id,
        run_id=f"run-{suffix}",
        attempt_id=f"attempt-{suffix}",
    )
    other = terminal.model_copy(update={"story_id": "live-story", "run_id": f"other-run-{suffix}"})
    docker = MagicMock()
    docker.inspect_container = AsyncMock(return_value=_container())
    docker.read_container_logs = AsyncMock(return_value="worker ending")
    docker.remove_container = AsyncMock()
    docker.remove_network = AsyncMock()
    boundary = _CanonicalWorkerManagerBoundary(redis, docker)
    try:
        for worker_id, owner in (
            (worker_ids["developer"], terminal),
            (worker_ids["qa_fix"], terminal),
            (worker_ids["other"], other),
        ):
            await redis.hset(
                f"worker:meta:{worker_id}",
                mapping={"worker_type": "developer", **owner.as_redis_meta()},
            )
            await redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})
        await redis.hset(STORY_WORKERS_KEY, "terminal-story", worker_ids["developer"])
        await redis.set(f"workspace:lock:{project_id}", worker_ids["developer"])

        story_api = _StoryAPI(terminal_status, project_id)
        assert await reconcile_terminal_story_workers(story_api, boundary) == 2

        removed = {call.args[0] for call in docker.remove_container.await_args_list}
        assert removed == {
            f"{settings.WORKER_IMAGE_PREFIX}-{worker_ids['developer']}",
            f"{settings.WORKER_IMAGE_PREFIX}-{worker_ids['qa_fix']}",
        }
        assert not await redis.exists(
            f"worker:status:{worker_ids['developer']}",
            f"worker:status:{worker_ids['qa_fix']}",
        )
        assert not await redis.exists(
            f"worker:meta:{worker_ids['developer']}",
            f"worker:meta:{worker_ids['qa_fix']}",
        )
        assert await redis.get(f"workspace:lock:{project_id}") is None
        assert await redis.hget(f"worker:status:{worker_ids['other']}", "status") == "RUNNING"
        assert await redis.hget(f"worker:meta:{worker_ids['other']}", "story_id") == "live-story"

        assert await reconcile_terminal_story_workers(story_api, boundary) == 0
        assert await redis.hget(STORY_WORKERS_KEY, "terminal-story") is None
        assert await reconcile_terminal_story_workers(story_api, boundary) == 0
        assert docker.remove_container.await_count == 2
    finally:
        await redis.delete(
            STORY_WORKERS_KEY,
            f"workspace:lock:{project_id}",
            *(f"worker:status:{worker_id}" for worker_id in worker_ids.values()),
            *(f"worker:error:{worker_id}" for worker_id in worker_ids.values()),
            *(f"worker:meta:{worker_id}" for worker_id in worker_ids.values()),
            *(removed_worker_evidence_key(run_id) for run_id in (terminal.run_id, other.run_id)),
        )
        await redis_client.close()
