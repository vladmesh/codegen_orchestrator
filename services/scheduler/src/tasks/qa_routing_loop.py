"""Independent scheduler loop for QA/testing routing."""

from __future__ import annotations

import asyncio

import structlog

from shared.redis import RedisStreamClient

from .. import runtime, startup
from .supervisor import supervise_testing_stories

logger = structlog.get_logger(__name__)


def _qa_routing_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def qa_routing_loop() -> None:
    """Route durable testing state without depending on dispatcher call order."""
    from ..clients.api import api_client

    async def cycle(redis_client: RedisStreamClient) -> dict[str, object]:
        return await supervise_testing_stories(api_client, redis_client)

    await runtime.periodic_loop(
        interval=_qa_routing_interval, cycle=cycle, logger=logger,
        started_event="qa_routing_started", cycle_event="qa_routing_cycle",
        error_event="qa_routing_cycle_error", stopped_event="qa_routing_stopped",
        redis_factory=RedisStreamClient, sleep=asyncio.sleep,
    )
