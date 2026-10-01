"""Independent scheduler loop for story-state supervision.

The state-age watchdog and the stage notices used to run last in the dispatcher
tick, because their correctness depended on every routing of that tick having
had its chance to move a story on first. It no longer does: the watchdog ends a
wait only through the API's compare-and-set on the status and anchor it read, and
a stage notice re-reads the story before it announces. Both act on durable state,
not on a position in the tick, so they run here on their own cadence and failure
boundary, and a failure on either side cannot stop the other loop.
"""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import runtime, startup
from .supervisor import supervise_stage_notices, supervise_state_age_bounds

logger = structlog.get_logger(__name__)


def _story_supervision_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def supervise_story_once(api_client, redis_client: RedisStreamClient) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        state_age = await supervise_state_age_bounds(api_client, redis_client)
        counts.update({f"state_age_{name}": n for name, n in state_age.items()})
    except Exception:
        logger.exception("story_supervision_cycle_error", sweep="state_age")
    try:
        stage_notices = await supervise_stage_notices(api_client, redis_client)
        counts.update({f"stage_notices_{name}": n for name, n in stage_notices.items()})
    except Exception:
        logger.exception("story_supervision_cycle_error", sweep="stage_notices")
    return counts


async def story_supervision_loop() -> None:
    from ..clients.api import api_client

    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return await supervise_story_once(api_client, redis_client)

    await runtime.periodic_loop(
        interval=_story_supervision_interval,
        cycle=cycle,
        logger=logger,
        started_event="story_supervision_started",
        cycle_event="story_supervision_cycle",
        error_event="story_supervision_cycle_error",
        stopped_event="story_supervision_stopped",
        redis_factory=RedisStreamClient,
        sleep=asyncio.sleep,
    )
