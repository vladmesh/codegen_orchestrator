"""Independent scheduler loop for owed owner-notification recovery."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import runtime, startup
from .owner_notifications import supervise_owed_owner_notifications

logger = structlog.get_logger(__name__)


def _owner_notification_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def owner_notification_loop() -> None:
    """Sweep owed owner notifications on their own cadence and failure boundary."""
    from ..clients.api import api_client

    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return await supervise_owed_owner_notifications(api_client, redis_client)

    await runtime.periodic_loop(
        interval=_owner_notification_interval,
        cycle=cycle,
        logger=logger,
        started_event="owner_notifications_started",
        cycle_event="owner_notifications_cycle",
        error_event="owner_notifications_cycle_error",
        stopped_event="owner_notifications_stopped",
        redis_factory=RedisStreamClient,
        sleep=asyncio.sleep,
    )
