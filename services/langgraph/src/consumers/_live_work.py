"""Live-test teardown fencing for capability worker executions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
import uuid

import structlog

from shared.clients.github import WorkflowCancellationUnprovenError
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)

LIVE_WORK_LEASE_SECONDS = 60
LIVE_WORK_LEASE_REFRESH_SECONDS = 10


def live_work_cancel_key(project_id: str) -> str:
    return f"live:work:cancelled:{project_id}"


def live_work_leases_key(project_id: str) -> str:
    return f"live:work:leases:{project_id}"


def live_work_failure_key(project_id: str) -> str:
    return f"live:work:failed:{project_id}"


async def _mark_live_work_failure(redis: RedisStreamClient, project_id: str, reason: str) -> None:
    """Leave a cleanup-visible fence when a cancelled stream entry cannot settle."""
    await redis.redis.set(live_work_failure_key(project_id), reason, ex=LIVE_WORK_LEASE_SECONDS * 2)


async def _begin_live_work(redis: RedisStreamClient, project_id: str) -> str | None:
    """Register a cancellable execution lease unless teardown fenced the project."""
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


async def _refresh_live_work_lease(redis: RedisStreamClient, project_id: str, token: str) -> bool:
    """Extend one lease atomically, or report that it was lost."""
    refreshed = await redis.redis.eval(
        """
        if redis.call('ZSCORE', KEYS[1], ARGV[1]) == false then return 0 end
        local now = redis.call('TIME')
        local expires = now[1] * 1000 + math.floor(now[2] / 1000) + ARGV[2] * 1000
        redis.call('ZADD', KEYS[1], 'XX', expires, ARGV[1])
        redis.call('EXPIRE', KEYS[1], ARGV[2] * 2)
        return 1
        """,
        1,
        live_work_leases_key(project_id),
        token,
        LIVE_WORK_LEASE_SECONDS,
    )
    return refreshed == 1


async def _cancel_on_live_teardown(
    redis: RedisStreamClient, project_id: str, token: str, owner: asyncio.Task[object]
) -> None:
    try:
        while True:
            await asyncio.sleep(LIVE_WORK_LEASE_REFRESH_SECONDS)
            if await redis.redis.exists(live_work_cancel_key(project_id)):
                owner.cancel()
                return
            if not await _refresh_live_work_lease(redis, project_id, token):
                await _mark_live_work_failure(redis, project_id, "lease_lost")
                owner.cancel()
                return
    except Exception:
        logger.error("live_work_watchdog_failed", project_id=project_id, exc_info=True)
        try:
            await _mark_live_work_failure(redis, project_id, "watchdog_failed")
        except Exception:
            logger.error(
                "live_work_failure_marker_write_failed", project_id=project_id, exc_info=True
            )
        owner.cancel()


async def execute_live_work(
    redis: RedisStreamClient,
    *,
    queue: str,
    group: str,
    message_id: str,
    project_id: str | None,
    process: Callable[[], Awaitable[dict]],
) -> dict | None:
    """Run one job with a live teardown lease and settle it fail-closed when cancelled."""
    if not project_id:
        result = await process()
        await redis.ack(queue, group, message_id)
        return result

    lease = await _begin_live_work(redis, project_id)
    if lease is None:
        await redis.ack(queue, group, message_id)
        logger.info("live_teardown_job_acked", entry_id=message_id)
        return None

    owner = asyncio.current_task()
    cancellation_watch = (
        asyncio.create_task(_cancel_on_live_teardown(redis, project_id, lease, owner))
        if owner is not None
        else None
    )
    try:
        result = await process()
        await redis.ack(queue, group, message_id)
        return result
    except asyncio.CancelledError:
        cancelled_by_teardown = await redis.redis.exists(live_work_cancel_key(project_id))
        cancelled_by_teardown = cancelled_by_teardown or await redis.redis.exists(
            live_work_failure_key(project_id)
        )
        if not cancelled_by_teardown:
            raise
        try:
            await redis.ack(queue, group, message_id)
        except Exception:
            await _mark_live_work_failure(redis, project_id, "ack_failed")
            raise
        logger.info("live_teardown_active_job_acked", entry_id=message_id)
        return None
    except WorkflowCancellationUnprovenError:
        # An external GitHub Actions run may still be live. This is fail-closed
        # regardless of which teardown key is set: never ACK, always fence cleanup.
        await _mark_live_work_failure(redis, project_id, "workflow_cancellation_unproven")
        raise
    except Exception:
        if await redis.redis.exists(live_work_cancel_key(project_id)):
            await _mark_live_work_failure(redis, project_id, "cancel_settlement_failed")
        raise
    finally:
        if cancellation_watch is not None:
            cancellation_watch.cancel()
            with suppress(asyncio.CancelledError):
                await cancellation_watch
        await _finish_live_work(redis, project_id, lease)
