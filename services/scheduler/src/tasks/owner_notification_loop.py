"""Independent scheduler loop for owed owner-notification recovery."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import startup
from .owner_notifications import supervise_owed_owner_notifications

logger = structlog.get_logger(__name__)


def _owner_notification_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def owner_notification_loop() -> None:
    """Sweep owed owner notifications on their own cadence and failure boundary.

    The cadence only decides how often records are visited. How often one record
    is attempted is its ``last_attempt_at`` claim at the API, whatever this loop does.
    """
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()
    logger.info("owner_notifications_started", interval=_owner_notification_interval())

    try:
        while True:
            try:
                counts = await supervise_owed_owner_notifications(api_client, redis_client)
                logger.info("owner_notifications_cycle", **counts)
            except Exception:
                logger.exception("owner_notifications_cycle_error")
            await asyncio.sleep(_owner_notification_interval())
    finally:
        await redis_client.close()
        logger.info("owner_notifications_stopped")
