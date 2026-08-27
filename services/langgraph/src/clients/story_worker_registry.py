"""Story worker registry — track which worker is alive for each story.

Uses a Redis hash (story:workers) mapping story_id → worker_id.
Engineering consumer writes after first spawn; scheduler cleans up
on story complete/fail.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.worker import WORKER_TERMINAL_STATUSES, WorkerStatus
from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from shared.redis import decode_redis_fields, decode_redis_value

if TYPE_CHECKING:
    import redis.asyncio as redis

logger = structlog.get_logger(__name__)

STALE_WORKER_CLEANUP_TIMEOUT_SECONDS = 30.0
STALE_WORKER_CLEANUP_POLL_SECONDS = 0.25


async def _clear_if_current(redis_client: redis.Redis, story_id: str, worker_id: str) -> bool:
    """Delete a story binding only if it still names the worker we inspected."""
    cleared = await redis_client.eval(
        """
        if redis.call('HGET', KEYS[1], ARGV[1]) == ARGV[2] then
            return redis.call('HDEL', KEYS[1], ARGV[1])
        end
        return 0
        """,
        1,
        STORY_WORKERS_KEY,
        story_id,
        worker_id,
    )
    return bool(cleared)


async def _wait_for_workspace_release(
    redis_client: redis.Redis, *, project_id: str | None, worker_id: str
) -> None:
    """Wait until stale-worker teardown no longer owns the project workspace."""
    if not project_id:
        return
    lock_key = f"workspace:lock:{project_id}"
    deadline = asyncio.get_running_loop().time() + STALE_WORKER_CLEANUP_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        holder = decode_redis_value(await redis_client.get(lock_key))
        if holder != worker_id:
            return
        await asyncio.sleep(STALE_WORKER_CLEANUP_POLL_SECONDS)
    raise RuntimeError(
        f"stale story worker {worker_id} still owns workspace for project {project_id}"
    )


async def get_story_worker(redis_client: redis.Redis, story_id: str) -> str | None:
    """Return a reusable story worker, cleaning a confirmed terminal one first.

    The story registry outlives a container crash. Reusing its raw value sends a
    new attempt to an input stream with no consumer; the supervisor then sees
    the dead worker on the new attempt and stops it. A terminal Docker status is
    sufficient evidence to remove that binding. Teardown must also release the
    owner-fenced workspace lock before the caller may spawn a replacement.

    An absent or unrecognised status remains inconclusive and is returned for
    the existing liveness waiter to handle. Redis uncertainty is not Docker
    proof and must not create two writers in one workspace.
    """
    value = await redis_client.hget(STORY_WORKERS_KEY, story_id)
    if value is None:
        return None
    worker_id = value.decode() if isinstance(value, bytes) else value

    raw_status = await redis_client.hget(f"worker:status:{worker_id}", "status")
    status_value = decode_redis_value(raw_status)
    try:
        status = WorkerStatus(status_value) if status_value is not None else None
    except ValueError:
        status = None
    if status not in WORKER_TERMINAL_STATUSES:
        return worker_id

    meta = decode_redis_fields(await redis_client.hgetall(f"worker:meta:{worker_id}"))
    cleared = await _clear_if_current(redis_client, story_id, worker_id)
    if not cleared:
        # Another writer replaced the binding after our read. Resolve the new
        # value instead of deleting or returning the worker it superseded.
        return await get_story_worker(redis_client, story_id)

    command = DeleteWorkerCommand(
        request_id=f"stale-story-{story_id}-{worker_id}",
        worker_id=worker_id,
        reason="failed",
    )
    await redis_client.xadd(WORKER_COMMANDS, {"data": command.model_dump_json()})
    logger.warning(
        "terminal_story_worker_evicted",
        story_id=story_id,
        worker_id=worker_id,
        status=status.value,
    )
    await _wait_for_workspace_release(
        redis_client,
        project_id=meta.get("project_id"),
        worker_id=worker_id,
    )
    return None


async def set_story_worker(redis_client: redis.Redis, story_id: str, worker_id: str) -> None:
    """Register a worker_id for a story."""
    await redis_client.hset(STORY_WORKERS_KEY, story_id, worker_id)
    logger.info("story_worker_registered", story_id=story_id, worker_id=worker_id)


async def clear_story_worker(redis_client: redis.Redis, story_id: str) -> None:
    """Remove the worker_id mapping for a story."""
    await redis_client.hdel(STORY_WORKERS_KEY, story_id)
    logger.info("story_worker_cleared", story_id=story_id)
