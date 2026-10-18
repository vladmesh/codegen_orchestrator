"""Publish scheduler wake hints on the native Redis Pub/Sub channel."""

from shared.contracts.queues.scheduler_wake import (
    SCHEDULER_WAKE_CHANNEL,
    SchedulerWakeHint,
    SchedulerWakeTarget,
)
from shared.redis.client import RedisStreamClient


async def publish_scheduler_wake(
    redis_client: RedisStreamClient, target: SchedulerWakeTarget
) -> int:
    """Publish one lossy hint after the durable write it announces; returns the receiver count."""
    hint = SchedulerWakeHint(target=target)
    return await redis_client.redis.publish(SCHEDULER_WAKE_CHANNEL, hint.model_dump_json())
