"""One scheduler-owned finalizer for worker removal and story bindings."""

from __future__ import annotations

import asyncio

import structlog

from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from shared.redis import RedisStreamClient, decode_redis_value

logger = structlog.get_logger(__name__)

# Worker-manager may spend 60 seconds in compose-down and another five proving
# that Docker removed the container. A scheduler pass observes only a bounded
# slice; the retained binding makes the next pass retry the same command.
TEARDOWN_OBSERVATIONS = 20
TEARDOWN_POLL_SECONDS = 0.25

_COMPARE_AND_DELETE_STORY_WORKER = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
if current == ARGV[2] then
    return redis.call('HDEL', KEYS[1], ARGV[1])
end
return 0
"""


async def _removal_observed(redis, worker_id: str, project_id: str | None) -> bool:
    status = await redis.hgetall(f"worker:status:{worker_id}")
    metadata = await redis.hgetall(f"worker:meta:{worker_id}")
    if status or metadata:
        return False
    if project_id:
        holder = decode_redis_value(await redis.get(f"workspace:lock:{project_id}"))
        if holder == worker_id:
            return False
    return True


async def finalize_story_worker_teardown(
    redis_client: RedisStreamClient,
    *,
    story_id: str | None,
    project_id: str | None,
    request_id: str,
    worker_id: str | None = None,
    reason: str = "completed",
    observations: int | None = None,
) -> bool:
    """Publish removal, observe its canonical evidence, then clear its binding.

    When ``worker_id`` is omitted, the exact current binding is the target. An
    explicit worker supports metadata-owned terminal workers and storyless
    administration while still compare-clearing it when it owns the binding.
    """
    redis = redis_client.redis
    initial_binding = None
    if story_id:
        initial_binding = decode_redis_value(await redis.hget(STORY_WORKERS_KEY, story_id))
    target = worker_id or initial_binding
    if not target:
        return True
    owned_binding = bool(story_id and initial_binding == target)

    command = DeleteWorkerCommand(request_id=request_id, worker_id=target, reason=reason)
    try:
        await redis_client.publish(WORKER_COMMANDS, command.model_dump(mode="json"))
    except Exception:
        logger.exception(
            "story_worker_teardown_publish_failed", story_id=story_id, worker_id=target
        )
        return False

    for observation in range(observations or TEARDOWN_OBSERVATIONS):
        if await _removal_observed(redis, target, project_id):
            if not story_id:
                return True
            current = decode_redis_value(await redis.hget(STORY_WORKERS_KEY, story_id))
            if current is None:
                return True
            if current != target:
                # An unbound historical worker may disappear beside a live
                # replacement. A worker that owned the binding at entry may
                # not authorize handoff across that replacement race.
                return not owned_binding
            cleared = await redis.eval(
                _COMPARE_AND_DELETE_STORY_WORKER,
                1,
                STORY_WORKERS_KEY,
                story_id,
                target,
            )
            if cleared:
                return True
            current = decode_redis_value(await redis.hget(STORY_WORKERS_KEY, story_id))
            return current is None or (current != target and not owned_binding)
        if observation + 1 < (observations or TEARDOWN_OBSERVATIONS):
            await asyncio.sleep(TEARDOWN_POLL_SECONDS)

    logger.warning(
        "story_worker_teardown_not_observed",
        story_id=story_id,
        worker_id=target,
        project_id=project_id,
    )
    return False
