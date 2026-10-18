"""Route native Pub/Sub wake hints to the scheduler loops that accept them."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from redis.exceptions import RedisError
import structlog

from shared.contracts.queues.scheduler_wake import (
    SCHEDULER_WAKE_CHANNEL,
    SchedulerWakeHintRejected,
    SchedulerWakeTarget,
    parse_scheduler_wake_hint,
)
from shared.redis import RedisStreamClient

from .. import startup

logger = structlog.get_logger(__name__)


def _reconnect_interval() -> int:
    return startup.get_config().get_int("scheduler.dispatch_interval_seconds")


async def scheduler_wake_listener(
    wakes: Mapping[SchedulerWakeTarget, asyncio.Event],
    *,
    redis_factory: Callable[[], RedisStreamClient] = RedisStreamClient,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Set the event of each valid hint's target; Redis failures reconnect after one interval.

    Every target in the vocabulary needs an event, so a hint never lacks its receiver.
    """
    if set(wakes) != set(SchedulerWakeTarget):
        raise ValueError(
            f"scheduler wake targets without an event: {set(SchedulerWakeTarget) - set(wakes)}"
        )
    while True:
        redis_client = redis_factory()
        try:
            await redis_client.connect()
            pubsub = redis_client.redis.pubsub()
            try:
                await pubsub.subscribe(SCHEDULER_WAKE_CHANNEL)
                logger.info("scheduler_wake_subscribed", channel=SCHEDULER_WAKE_CHANNEL)
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        _route(message["data"], wakes)
                logger.warning("scheduler_wake_subscription_ended")
            finally:
                await pubsub.aclose()
        except (RedisError, OSError) as exc:
            logger.warning("scheduler_wake_listener_disconnected", error_type=type(exc).__name__)
        finally:
            await redis_client.close()
        await sleep(_reconnect_interval())


def _route(data: object, wakes: Mapping[SchedulerWakeTarget, asyncio.Event]) -> None:
    try:
        hint = parse_scheduler_wake_hint(data)
    except SchedulerWakeHintRejected as rejected:
        logger.warning("scheduler_wake_hint_rejected", **rejected.diagnostics)
        return
    wakes[hint.target].set()
