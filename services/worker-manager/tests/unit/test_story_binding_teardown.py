"""The story binding dies with the worker it names.

`delete_worker` deletes `worker:status:<id>` and `worker:meta:<id>`, which are
the only evidence anyone else could have evicted the binding from. So teardown
is the place that unbinds the story: after this method there is nothing left to
learn the worker is gone from, and a binding that outlives its worker sends the
next engineering attempt to an input stream with no consumer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fakeredis import aioredis
import pytest

from shared.contracts.queues.worker import WorkerOwnership
from shared.queues import STORY_WORKERS_KEY
from src.worker_removal import WorkerRemoval

pytestmark = pytest.mark.asyncio

OWNERSHIP = WorkerOwnership(
    story_id="story-ea07a289", project_id="proj-1", run_id="live-1", attempt_id="eng-1"
)


def _docker_double() -> MagicMock:
    docker = MagicMock()
    docker.inspect_container = AsyncMock(
        return_value={
            "Image": "sha256:" + "1" * 64,
            "Config": {"Image": "worker:latest", "Env": ["WORKER_AGENT_TYPE=codex"]},
            "State": {
                "Status": "running",
                "Running": True,
                "OOMKilled": False,
                "ExitCode": 0,
                "StartedAt": "2026-09-17T18:13:28Z",
                "FinishedAt": "0001-01-01T00:00:00Z",
                "Error": "",
            },
            "Mounts": [],
        }
    )
    docker.read_container_logs = AsyncMock(return_value="checkout_branch_start\n")
    docker.remove_container = AsyncMock()
    docker.remove_network = AsyncMock()
    return docker


def _removal(redis) -> WorkerRemoval:
    return WorkerRemoval(
        redis,
        _docker_double(),
        unregister_broker_worker=AsyncMock(),
        release_workspace_lock=AsyncMock(),
    )


async def _owned_worker(redis, worker_id: str, ownership: WorkerOwnership) -> None:
    await redis.hset(
        f"worker:meta:{worker_id}",
        mapping={"worker_type": "developer", **ownership.as_redis_meta()},
    )
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})


async def test_deleting_a_worker_unbinds_only_its_own_story():
    redis = aioredis.FakeRedis(decode_responses=True)
    other = OWNERSHIP.model_copy(update={"story_id": "story-other", "run_id": "live-2"})
    await _owned_worker(redis, "dev-p-1", OWNERSHIP)
    await _owned_worker(redis, "dev-p-2", other)
    await redis.hset(
        STORY_WORKERS_KEY,
        mapping={OWNERSHIP.story_id: "dev-p-1", other.story_id: "dev-p-2"},
    )

    await _removal(redis).delete_worker("dev-p-1", reason="failed")

    assert await redis.hget(STORY_WORKERS_KEY, OWNERSHIP.story_id) is None
    assert await redis.hget(STORY_WORKERS_KEY, other.story_id) == "dev-p-2"


async def test_a_binding_already_taken_over_by_a_newer_worker_is_left_alone():
    """Compare-and-delete: the live worker's binding is not this teardown's to remove."""
    redis = aioredis.FakeRedis(decode_responses=True)
    await _owned_worker(redis, "dev-p-old", OWNERSHIP)
    await redis.hset(STORY_WORKERS_KEY, OWNERSHIP.story_id, "dev-p-new")

    await _removal(redis).delete_worker("dev-p-old", reason="failed")

    assert await redis.hget(STORY_WORKERS_KEY, OWNERSHIP.story_id) == "dev-p-new"


async def test_a_storyless_worker_touches_no_binding():
    redis = aioredis.FakeRedis(decode_responses=True)
    standalone = OWNERSHIP.model_copy(update={"story_id": None})
    await _owned_worker(redis, "dev-p-standalone", standalone)
    await redis.hset(STORY_WORKERS_KEY, OWNERSHIP.story_id, "dev-p-1")

    await _removal(redis).delete_worker("dev-p-standalone", reason="completed")

    assert await redis.hget(STORY_WORKERS_KEY, OWNERSHIP.story_id) == "dev-p-1"


async def test_a_failed_removal_leaves_the_binding_in_place():
    """A Docker failure is not a teardown confirmation, so nothing is unbound."""
    redis = aioredis.FakeRedis(decode_responses=True)
    await _owned_worker(redis, "dev-p-1", OWNERSHIP)
    await redis.hset(STORY_WORKERS_KEY, OWNERSHIP.story_id, "dev-p-1")
    removal = _removal(redis)
    removal.docker.remove_container = AsyncMock(side_effect=RuntimeError("daemon unreachable"))

    await removal.delete_worker("dev-p-1", reason="failed")

    assert await redis.hget(STORY_WORKERS_KEY, OWNERSHIP.story_id) == "dev-p-1"
