"""Independent scheduler loop for story completion and PR creation."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import startup
from .story_completion import complete_stories

logger = structlog.get_logger(__name__)


def _story_completion_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def story_completion_loop() -> None:
    """Complete eligible stories from durable task/story state."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()
    logger.info("story_completion_started", interval=_story_completion_interval())

    try:
        while True:
            try:
                completed = await complete_stories(api_client, redis_client)
                logger.info("story_completion_cycle", stories_completed=completed)
            except Exception:
                logger.exception("story_completion_cycle_error")
            await asyncio.sleep(_story_completion_interval())
    finally:
        await redis_client.close()
        logger.info("story_completion_stopped")
