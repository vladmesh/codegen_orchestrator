"""Independent scheduler loop for scaffold triggering."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import startup
from .scaffold_trigger import trigger_scaffolds

logger = structlog.get_logger(__name__)


def _scaffold_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def scaffold_loop() -> None:
    """Trigger scaffolds on their own cadence and failure boundary.

    Dispatch admission independently fails closed while a project is not
    scaffolded or its workspace is not ready, so correctness does not depend on
    scaffold triggering running earlier in the same scheduler tick.
    """
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()
    logger.info("scaffold_loop_started", interval=_scaffold_interval())

    try:
        while True:
            try:
                triggered = await trigger_scaffolds(api_client, redis_client)
                logger.info("scaffold_cycle", scaffolds_triggered=triggered)
            except Exception:
                logger.exception("scaffold_cycle_error")
            await asyncio.sleep(_scaffold_interval())
    finally:
        await redis_client.close()
        logger.info("scaffold_loop_stopped")
