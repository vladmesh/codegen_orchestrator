"""Redis Streams job queues for async task processing.

Provides queue constants and utilities for Phase 4 capability workers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = structlog.get_logger(__name__)

# Queue stream names (Phase 6: CLI-triggered queues)
DEPLOY_QUEUE = "deploy:queue"
ENGINEERING_QUEUE = "engineering:queue"
ADMIN_QUEUE = "admin:queue"

# Consumer group name (shared across all workers)
WORKER_GROUP = "capability-workers"

# Job retention TTL in seconds (7 days)
JOB_TTL_SECONDS = 7 * 24 * 60 * 60


async def ensure_consumer_groups(redis: Redis) -> None:
    """Create consumer groups if they don't exist.

    Should be called on worker startup.

    Args:
        redis: Connected Redis client
    """
    queues = [DEPLOY_QUEUE, ENGINEERING_QUEUE, ADMIN_QUEUE]

    for queue in queues:
        try:
            await redis.xgroup_create(
                queue,
                WORKER_GROUP,
                id="0",
                mkstream=True,
            )
            logger.info("consumer_group_created", queue=queue, group=WORKER_GROUP)
        except Exception as e:
            if "BUSYGROUP" in str(e):
                # Group already exists
                logger.debug("consumer_group_exists", queue=queue, group=WORKER_GROUP)
            else:
                logger.error(
                    "consumer_group_creation_failed",
                    queue=queue,
                    error=str(e),
                )
                raise


async def get_pending_job_count(redis: Redis, queue: str) -> int:
    """Get count of pending (unacked) jobs in a queue.

    Args:
        redis: Connected Redis client
        queue: Queue stream name

    Returns:
        Number of pending jobs
    """
    try:
        info = await redis.xpending(queue, WORKER_GROUP)
        return info.get("pending", 0) if isinstance(info, dict) else 0
    except Exception:
        return 0


async def get_user_active_jobs(redis: Redis, queue: str, user_id: int) -> int:
    """Count active jobs for a specific user in a queue.

    Used to enforce per-user concurrency limits.

    Args:
        redis: Connected Redis client
        queue: Queue stream name
        user_id: Telegram user ID

    Returns:
        Number of active jobs for this user
    """
    # Read recent entries and count by user
    # This is approximate - for exact count would need secondary index
    try:
        entries = await redis.xrevrange(queue, count=100)
        count = 0
        for _entry_id, data in entries:
            if data.get("user_id") == str(user_id):
                count += 1
        return count
    except Exception:
        return 0
