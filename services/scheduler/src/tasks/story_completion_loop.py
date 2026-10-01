"""Independent scheduler loop for story completion and PR creation."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import runtime, startup
from .story_completion import complete_stories

logger = structlog.get_logger(__name__)


def _story_completion_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def story_completion_loop() -> None:
    """Complete eligible stories from durable task/story state."""
    from ..clients.api import api_client

    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return {"stories_completed": await complete_stories(api_client, redis_client)}

    await runtime.periodic_loop(
        interval=_story_completion_interval, cycle=cycle, logger=logger,
        started_event="story_completion_started", cycle_event="story_completion_cycle",
        error_event="story_completion_cycle_error", stopped_event="story_completion_stopped",
        redis_factory=RedisStreamClient, sleep=asyncio.sleep,
    )
