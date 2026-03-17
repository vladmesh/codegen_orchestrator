"""Story worker registry — track which worker is alive for each story.

Uses a Redis hash (story:workers) mapping story_id → worker_id.
Engineering consumer writes after first spawn; scheduler cleans up
on story complete/fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import redis.asyncio as redis

from shared.queues import STORY_WORKERS_KEY

logger = structlog.get_logger(__name__)


async def get_story_worker(redis_client: redis.Redis, story_id: str) -> str | None:
    """Look up the active worker_id for a story. Returns None if not found."""
    value = await redis_client.hget(STORY_WORKERS_KEY, story_id)
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else value


async def set_story_worker(redis_client: redis.Redis, story_id: str, worker_id: str) -> None:
    """Register a worker_id for a story."""
    await redis_client.hset(STORY_WORKERS_KEY, story_id, worker_id)
    logger.info("story_worker_registered", story_id=story_id, worker_id=worker_id)


async def clear_story_worker(redis_client: redis.Redis, story_id: str) -> None:
    """Remove the worker_id mapping for a story."""
    await redis_client.hdel(STORY_WORKERS_KEY, story_id)
    logger.info("story_worker_cleared", story_id=story_id)
