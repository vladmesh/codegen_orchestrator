"""Base worker loop for Redis Stream queue consumers.

Provides common boilerplate shared by engineering_worker and deploy_worker:
signal handling, consumer group setup, message reading, ACKing, and shutdown.

Includes a staleness guard: before processing, checks if the referenced run/story
is already terminal (COMPLETED/FAILED/CANCELLED/ARCHIVED). If so, ACKs and skips.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import os
import signal
import time
import uuid

from pydantic import ValidationError
import structlog

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story import StoryStatus
from shared.log_config import setup_logging
from shared.log_config.correlation import bind_message_context, unbind_message_context
from shared.queues import WORKER_GROUP
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ._validation import _safe_validation_errors

logger = structlog.get_logger(__name__)

# Type alias for job processor functions
ProcessFn = Callable[[dict, RedisStreamClient], Awaitable[dict]]


class TerminalMessageValidationError(Exception):
    """Validation failure raised only while parsing the Redis message."""

    def __init__(self, validation_error: ValidationError) -> None:
        self.validation_error = validation_error
        super().__init__("invalid queue message")


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
LIVE_WORK_LEASE_SECONDS = 60
LIVE_WORK_LEASE_REFRESH_SECONDS = 10


def live_work_cancel_key(project_id: str) -> str:
    return f"live:work:cancelled:{project_id}"


def live_work_leases_key(project_id: str) -> str:
    return f"live:work:leases:{project_id}"


async def _begin_live_work(redis: RedisStreamClient, project_id: str) -> str | None:
    """Register a cancellable execution lease unless live teardown has fenced the project."""
    token = uuid.uuid4().hex
    registered = await redis.redis.eval(
        """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
        local now = redis.call('TIME')
        local expires = now[1] * 1000 + math.floor(now[2] / 1000) + ARGV[2] * 1000
        redis.call('ZADD', KEYS[2], expires, ARGV[1])
        redis.call('EXPIRE', KEYS[2], ARGV[2] * 2)
        return 1
        """,
        2,
        live_work_cancel_key(project_id),
        live_work_leases_key(project_id),
        token,
        LIVE_WORK_LEASE_SECONDS,
    )
    return token if registered == 1 else None


async def _finish_live_work(redis: RedisStreamClient, project_id: str, token: str) -> None:
    await redis.redis.zrem(live_work_leases_key(project_id), token)


async def _cancel_on_live_teardown(
    redis: RedisStreamClient, project_id: str, token: str, owner: asyncio.Task[object]
) -> None:
    while True:
        await asyncio.sleep(LIVE_WORK_LEASE_REFRESH_SECONDS)
        if await redis.redis.exists(live_work_cancel_key(project_id)):
            owner.cancel()
            return
        now = int(time.time() * 1000)
        await redis.redis.zadd(
            live_work_leases_key(project_id), {token: now + LIVE_WORK_LEASE_SECONDS * 1000}
        )


def validate_queued_message(model, job_data: dict):
    """Parse a queue payload and mark only input validation as terminal."""
    try:
        return model.model_validate(job_data)
    except ValidationError as exc:
        raise TerminalMessageValidationError(exc) from exc


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
) -> None:  # noqa: PLR0915
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

                project_id = msg.data.get("project_id")
                lease = None
                cancellation_watch = None
                if isinstance(project_id, str) and project_id:
                    lease = await _begin_live_work(redis, project_id)
                    if lease is None:
                        await redis.ack(queue, group, msg.message_id)
                        logger.info("live_teardown_job_acked", entry_id=msg.message_id)
                        continue
                    owner = asyncio.current_task()
                    if owner is not None:
                        cancellation_watch = asyncio.create_task(
                            _cancel_on_live_teardown(redis, project_id, lease, owner)
                        )

                try:
                    result = await process_fn(msg.data, redis)
                    msg.data.update(result)
                    await redis.ack(queue, group, msg.message_id)
                    logger.debug("job_acked", entry_id=msg.message_id, worker=service_name)
                except asyncio.CancelledError:
                    cancelled_by_teardown = project_id and await redis.redis.exists(
                        live_work_cancel_key(project_id)
                    )
                    if not cancelled_by_teardown:
                        raise
                    await redis.ack(queue, group, msg.message_id)
                    logger.info("live_teardown_active_job_acked", entry_id=msg.message_id)
                finally:
                    if cancellation_watch is not None:
                        cancellation_watch.cancel()
                        with suppress(asyncio.CancelledError):
                            await cancellation_watch
                    if lease is not None:
                        await _finish_live_work(redis, project_id, lease)
            except TerminalMessageValidationError as exc:
                # A schema error cannot become valid when reclaimed from the PEL.
                # ACK it after recording a payload-safe terminal diagnostic.
                logger.error(
                    "terminal_message_validation_failed",
                    entry_id=msg.message_id,
                    worker=service_name,
                    errors=_safe_validation_errors(exc.validation_error),
                )
                try:
                    await redis.ack(queue, group, msg.message_id)
                except Exception as ack_exc:
                    logger.error(
                        "terminal_message_ack_failed",
                        entry_id=msg.message_id,
                        worker=service_name,
                        error_type=type(ack_exc).__name__,
                        exc_info=True,
                    )
            except Exception as exc:
                logger.error(
                    "job_processing_error",
                    entry_id=msg.message_id,
                    error_type=type(exc).__name__,
                    worker=service_name,
                    exc_info=True,
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
