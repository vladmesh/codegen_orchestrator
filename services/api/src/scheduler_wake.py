"""Best-effort scheduler wake hints published by api routes after their own commit."""

from redis.exceptions import RedisError
import structlog

from shared.contracts.queues.scheduler_wake import SchedulerWakeTarget
from shared.redis.client import RedisStreamClient
from shared.redis.scheduler_wake import publish_scheduler_wake

logger = structlog.get_logger()


async def publish_wake(
    redis: RedisStreamClient, target: SchedulerWakeTarget, **subject: object
) -> None:
    """Publish one hint after the caller's commit; a lost hint waits for the sweep's interval.

    `subject` identifies the committed row in the diagnostics, which name an error by type only.
    """
    try:
        receivers = await publish_scheduler_wake(redis, target)
    except (RedisError, OSError) as exc:
        logger.warning(f"{target.value}_wake_failed", **subject, error_type=type(exc).__name__)
        return
    logger.info(f"{target.value}_wake_published", **subject, receivers=receivers)
