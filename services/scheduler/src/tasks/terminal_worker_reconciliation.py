"""Story-scoped worker teardown for every terminal story."""

from __future__ import annotations

import structlog

from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.queues import STORY_WORKERS_KEY, WORKER_COMMANDS
from shared.redis import decode_redis_fields, decode_redis_value

logger = structlog.get_logger(__name__)

TERMINAL_STORY_STATUSES = (
    StoryStatus.COMPLETED,
    StoryStatus.FAILED,
    StoryStatus.ARCHIVED,
)


async def reconcile_terminal_story_workers(api_client, redis_client) -> int:
    """Request canonical teardown for every worker a terminal story owns.

    Worker metadata is the durable retry record. It is deliberately not removed
    here: worker-manager removes it only after Docker confirms teardown. The
    legacy story registry is also retained until its worker has disappeared,
    which makes pre-``story_id`` workers drainable when that binding proves the
    owner while leaving every ambiguous legacy worker protected.
    """
    terminal_story_ids: set[str] = set()
    for status in TERMINAL_STORY_STATUSES:
        stories = await api_client.get_stories_by_status(status)
        terminal_story_ids.update(story.id for story in stories)

    if not terminal_story_ids:
        return 0

    workers_by_story: dict[str, set[str]] = {story_id: set() for story_id in terminal_story_ids}
    async for raw_key in redis_client.redis.scan_iter(match="worker:meta:*"):
        key = decode_redis_value(raw_key)
        meta = decode_redis_fields(await redis_client.redis.hgetall(key))
        story_id = meta.get("story_id")
        if story_id in workers_by_story:
            workers_by_story[story_id].add(key.removeprefix("worker:meta:"))

    for story_id in terminal_story_ids:
        raw_bound = await redis_client.redis.hget(STORY_WORKERS_KEY, story_id)
        bound_worker = decode_redis_value(raw_bound)
        if not bound_worker:
            continue
        meta_exists = bool(await redis_client.redis.hgetall(f"worker:meta:{bound_worker}"))
        status_exists = bool(
            await redis_client.redis.hget(f"worker:status:{bound_worker}", "status")
        )
        if meta_exists or status_exists:
            workers_by_story[story_id].add(bound_worker)
        else:
            await redis_client.redis.hdel(STORY_WORKERS_KEY, story_id)

    requested = 0
    for story_id, worker_ids in workers_by_story.items():
        for worker_id in sorted(worker_ids):
            command = DeleteWorkerCommand(
                request_id=f"terminal-story-{story_id}-{worker_id}",
                worker_id=worker_id,
                reason="completed",
            )
            try:
                await redis_client.publish(WORKER_COMMANDS, command.model_dump(mode="json"))
            except Exception:
                logger.exception(
                    "terminal_story_worker_teardown_publish_failed",
                    story_id=story_id,
                    worker_id=worker_id,
                )
                continue
            requested += 1
            logger.info(
                "terminal_story_worker_teardown_requested",
                story_id=story_id,
                worker_id=worker_id,
            )
    return requested
