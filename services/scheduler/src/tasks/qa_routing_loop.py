"""Independent scheduler loop for QA/testing routing."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import startup
from .supervisor import supervise_testing_stories

logger = structlog.get_logger(__name__)


def _qa_routing_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def qa_routing_loop() -> None:
    """Route durable testing state without depending on dispatcher call order."""
    from ..clients.api import api_client

    redis_client = RedisStreamClient()
    await redis_client.connect()
    logger.info("qa_routing_started", interval=_qa_routing_interval())

    try:
        while True:
            try:
                counts = await supervise_testing_stories(api_client, redis_client)
                logger.info("qa_routing_cycle", **counts)
            except Exception:
                logger.exception("qa_routing_cycle_error")
            await asyncio.sleep(_qa_routing_interval())
    finally:
        await redis_client.close()
        logger.info("qa_routing_stopped")
