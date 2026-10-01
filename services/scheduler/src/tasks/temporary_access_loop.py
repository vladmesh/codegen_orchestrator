"""Independent scheduler loop for durable temporary QA access cleanup."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import runtime, startup
from .temporary_access import supervise_temporary_access

logger = structlog.get_logger(__name__)


def _temporary_access_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def temporary_access_loop() -> None:
    """Sweep temporary access on its own cadence and failure boundary."""
    from ..clients.api import api_client

    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return await supervise_temporary_access(api_client, redis_client)

    await runtime.periodic_loop(
        interval=_temporary_access_interval,
        cycle=cycle,
        logger=logger,
        started_event="temporary_access_started",
        cycle_event="temporary_access_cycle",
        error_event="temporary_access_cycle_error",
        stopped_event="temporary_access_stopped",
        redis_factory=RedisStreamClient,
        sleep=asyncio.sleep,
    )
