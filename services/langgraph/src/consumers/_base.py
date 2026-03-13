"""Base worker loop for Redis Stream queue consumers.

Provides common boilerplate shared by engineering_worker and deploy_worker:
signal handling, consumer group setup, message reading, ACKing, and shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import os
import signal

import structlog

from shared.log_config import setup_logging
from shared.log_config.correlation import bind_message_context, unbind_message_context
from shared.queues import WORKER_GROUP
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client

logger = structlog.get_logger(__name__)

# Type alias for job processor functions
ProcessFn = Callable[[dict, RedisStreamClient], Awaitable[dict]]

# Module-level shutdown flag (set by signal handler)
_shutdown = False


def _handle_shutdown(signum, _frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def run_queue_worker(
    service_name: str,
    queue: str,
    process_fn: ProcessFn,
    group: str = WORKER_GROUP,
) -> None:
    """Generic worker loop for Redis Stream queue consumption.

    Args:
        service_name: Name for logging and consumer identification
        queue: Redis Stream queue name to consume from
        process_fn: Async function(job_data, redis) -> result dict
        group: Consumer group name (defaults to WORKER_GROUP)
    """
    global _shutdown
    _shutdown = False

    setup_logging(service_name=service_name)

    consumer_name = f"{service_name}-{os.getpid()}"

    redis = RedisStreamClient()
    await redis.connect()

    logger.info(f"{service_name}_started", consumer=consumer_name)

    try:
        async for msg in redis.consume(
            queue,
            group,
            consumer_name,
            auto_ack=False,
            claim_pending=True,
        ):
            if _shutdown:
                break
            if msg is None:
                continue
            try:
                bind_message_context(msg.data)
                result = await process_fn(msg.data, redis)
                msg.data.update(result)
                await redis.ack(queue, group, msg.message_id)
                logger.debug("job_acked", entry_id=msg.message_id, worker=service_name)
            except Exception as e:
                logger.error(
                    "job_processing_error",
                    entry_id=msg.message_id,
                    error=str(e),
                    worker=service_name,
                )
            finally:
                unbind_message_context()
    finally:
        await redis.close()
        await api_client.close()
        logger.info(f"{service_name}_shutdown")


def start_worker(
    service_name: str,
    queue: str,
    process_fn: ProcessFn,
    group: str = WORKER_GROUP,
) -> None:
    """Entry point: register signal handlers and run the worker loop.

    Args:
        service_name: Name for logging and consumer identification
        queue: Redis Stream queue name to consume from
        process_fn: Async function(job_data, redis) -> result dict
        group: Consumer group name (defaults to WORKER_GROUP)
    """
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    asyncio.run(run_queue_worker(service_name, queue, process_fn, group=group))
