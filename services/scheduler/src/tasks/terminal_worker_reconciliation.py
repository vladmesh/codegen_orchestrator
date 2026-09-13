"""Story-scoped worker teardown for every terminal story."""

from __future__ import annotations

import structlog

from shared.contracts.dto.story import StoryStatus
from shared.queues import STORY_WORKERS_KEY
from shared.redis import decode_redis_fields, decode_redis_value

from .story_worker_teardown import finalize_story_worker_teardown

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
    terminal_stories: dict[str, str] = {}
    for status in TERMINAL_STORY_STATUSES:
        stories = await api_client.get_stories_by_status(status)
        terminal_stories.update((story.id, str(story.project_id)) for story in stories)

    if not terminal_stories:
        return 0

    workers_by_story: dict[str, set[str]] = {story_id: set() for story_id in terminal_stories}
    async for raw_key in redis_client.redis.scan_iter(match="worker:meta:*"):
        key = decode_redis_value(raw_key)
        meta = decode_redis_fields(await redis_client.redis.hgetall(key))
        story_id = meta.get("story_id")
        if story_id in workers_by_story:
            workers_by_story[story_id].add(key.removeprefix("worker:meta:"))

    bound_workers: dict[str, str] = {}
    for story_id in terminal_stories:
        raw_bound = await redis_client.redis.hget(STORY_WORKERS_KEY, story_id)
        bound_worker = decode_redis_value(raw_bound)
        if bound_worker:
            bound_workers[story_id] = bound_worker
            workers_by_story[story_id].add(bound_worker)

    finalized = 0
    for story_id, worker_ids in workers_by_story.items():
        bound_worker = bound_workers.get(story_id)
        ordered = sorted(worker_ids, key=lambda worker_id: worker_id != bound_worker)
        for worker_id in ordered:
            complete = await finalize_story_worker_teardown(
                redis_client,
                story_id=story_id,
                project_id=terminal_stories[story_id],
                request_id=f"terminal-story-{story_id}-{worker_id}",
                worker_id=worker_id,
                observations=1,
            )
            if not complete:
                continue
            finalized += 1
            logger.info(
                "terminal_story_worker_teardown_finalized",
                story_id=story_id,
                worker_id=worker_id,
            )
    return finalized
