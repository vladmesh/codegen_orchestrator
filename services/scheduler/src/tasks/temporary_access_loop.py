"""Independent scheduler loop for durable temporary QA access cleanup."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import startup
from .temporary_access import supervise_temporary_access

logger = structlog.get_logger(__name__)


def _temporary_access_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def temporary_access_loop() -> None:
    """Sweep temporary access on its own cadence and failure boundary."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()
    logger.info("temporary_access_started", interval=_temporary_access_interval())

    try:
        while True:
            try:
                counts = await supervise_temporary_access(api_client, redis_client)
                logger.info("temporary_access_cycle", **counts)
            except Exception:
                logger.exception("temporary_access_cycle_error")
            await asyncio.sleep(_temporary_access_interval())
    finally:
        await redis_client.close()
        logger.info("temporary_access_stopped")
