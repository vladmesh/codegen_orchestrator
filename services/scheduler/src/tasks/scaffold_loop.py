"""Independent scheduler loop for scaffold triggering."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import runtime, startup
from .scaffold_trigger import trigger_scaffolds

logger = structlog.get_logger(__name__)


def _scaffold_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def scaffold_loop() -> None:
    """Trigger scaffolds on their own cadence and failure boundary."""
    from ..clients.api import api_client

    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return {"scaffolds_triggered": await trigger_scaffolds(api_client, redis_client)}

    await runtime.periodic_loop(
        interval=_scaffold_interval, cycle=cycle, logger=logger,
        started_event="scaffold_loop_started", cycle_event="scaffold_cycle",
        error_event="scaffold_cycle_error", stopped_event="scaffold_loop_stopped",
        redis_factory=RedisStreamClient, sleep=asyncio.sleep,
    )
