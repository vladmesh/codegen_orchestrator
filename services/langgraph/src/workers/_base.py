"""Base worker loop for Redis Stream queue consumers.

Provides common boilerplate shared by engineering_worker and deploy_worker:
signal handling, consumer group setup, message reading, ACKing, and shutdown.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import os
import signal

import structlog

from shared.log_config import setup_logging
from shared.queues import WORKER_GROUP, ensure_consumer_groups
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
) -> None:
    """Generic worker loop for Redis Stream queue consumption.

    Args:
        service_name: Name for logging and consumer identification
        queue: Redis Stream queue name to consume from
        process_fn: Async function(job_data, redis) -> result dict
    """
    global _shutdown
    _shutdown = False

    setup_logging(service_name=service_name)

    consumer_name = f"{service_name}-{os.getpid()}"

    redis = RedisStreamClient()
    await redis.connect()

    await ensure_consumer_groups(redis.redis)

    logger.info(f"{service_name}_started", consumer=consumer_name)

    try:
        while not _shutdown:
            try:
                messages = await redis.redis.xreadgroup(
                    groupname=WORKER_GROUP,
                    consumername=consumer_name,
                    streams={queue: ">"},
                    count=1,
                    block=5000,
                )

                if not messages:
                    continue

                for _stream_name, entries in messages:
                    for entry_id, raw_data in entries:
                        try:
                            if "data" in raw_data:
                                job_data = json.loads(raw_data["data"])
                            else:
                                job_data = raw_data

                            result = await process_fn(job_data, redis)
                            job_data.update(result)

                            await redis.redis.xack(queue, WORKER_GROUP, entry_id)
                            logger.debug("job_acked", entry_id=entry_id, worker=service_name)

                        except Exception as e:
                            logger.error(
                                "job_processing_error",
                                entry_id=entry_id,
                                error=str(e),
                                worker=service_name,
                            )

            except asyncio.CancelledError:
                logger.info("worker_cancelled", worker=service_name)
                break
            except Exception as e:
                if "NOGROUP" in str(e):
                    logger.warning(
                        "consumer_nogroup_recovering",
                        stream=queue,
                        group=WORKER_GROUP,
                        worker=service_name,
                    )
                    await ensure_consumer_groups(redis.redis)
                else:
                    logger.error("worker_loop_error", error=str(e), worker=service_name)
                await asyncio.sleep(1)

    finally:
        await redis.close()
        await api_client.close()
        logger.info(f"{service_name}_shutdown")


def start_worker(
    service_name: str,
    queue: str,
    process_fn: ProcessFn,
) -> None:
    """Entry point: register signal handlers and run the worker loop.

    Args:
        service_name: Name for logging and consumer identification
        queue: Redis Stream queue name to consume from
        process_fn: Async function(job_data, redis) -> result dict
    """
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    asyncio.run(run_queue_worker(service_name, queue, process_fn))
