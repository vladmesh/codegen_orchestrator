"""Base worker loop for Redis Stream queue consumers.

Provides common boilerplate shared by engineering_worker and deploy_worker:
signal handling, consumer group setup, message reading, ACKing, and shutdown.

Includes a staleness guard: before processing, checks if the referenced run/story
is already terminal (COMPLETED/FAILED/CANCELLED/ARCHIVED). If so, ACKs and skips.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import os
import signal

import structlog

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story import StoryStatus
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

# Terminal statuses — messages referencing these are stale
_TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
}
_TERMINAL_STORY_STATUSES = {
    StoryStatus.COMPLETED.value,
    StoryStatus.FAILED.value,
    StoryStatus.ARCHIVED.value,
}


def _handle_shutdown(signum, _frame):
    """Handle shutdown signals gracefully."""
    global _shutdown
    logger.info("shutdown_signal_received", signal=signum)
    _shutdown = True


async def _check_message_staleness(job_data: dict) -> bool:
    """Check if a queue message references a terminal run or story.

    Returns True if the message is stale and should be skipped.
    On API errors, returns False (proceed with processing).
    """
    task_id = job_data.get("task_id")
    if task_id:
        try:
            run_data = await api_client.get(f"runs/{task_id}")
            if run_data["status"] in _TERMINAL_RUN_STATUSES:
                logger.info(
                    "stale_message_skipped",
                    task_id=task_id,
                    run_status=run_data["status"],
                    reason="run_terminal",
                )
                return True
        except Exception:
            logger.debug("staleness_guard_api_error", task_id=task_id, exc_info=True)
        return False

    story_id = job_data.get("story_id")
    if story_id:
        try:
            story = await api_client.get_story(story_id)
            if story.status in _TERMINAL_STORY_STATUSES:
                logger.info(
                    "stale_message_skipped",
                    story_id=story_id,
                    story_status=story.status,
                    reason="story_terminal",
                )
                return True
        except Exception:
            logger.debug("staleness_guard_api_error", story_id=story_id, exc_info=True)
        return False

    return False


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

                # Staleness guard: skip messages for terminal runs/stories
                if await _check_message_staleness(msg.data):
                    await redis.ack(queue, group, msg.message_id)
                    logger.debug("stale_job_acked", entry_id=msg.message_id, worker=service_name)
                    continue

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
