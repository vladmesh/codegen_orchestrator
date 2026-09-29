"""Live-test teardown fencing for capability worker executions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import nullcontext, suppress
from dataclasses import dataclass
import time
import uuid

from redis.exceptions import (
    AuthenticationError as RedisAuthenticationError,
    AuthorizationError as RedisAuthorizationError,
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)
import structlog

from shared.clients.github import WorkflowCancellationUnprovenError
from shared.redis import RedisStreamClient

logger = structlog.get_logger(__name__)

LIVE_WORK_LEASE_SECONDS = 60
LIVE_WORK_LEASE_REFRESH_SECONDS = 10
LIVE_WORK_SETTLED_KEY = "_live_work_settled"
_COMMIT_UNSETTLED = -1
_COMMIT_LEASE_LOST = -2
_COMMIT_UNFENCED = -3


class LiveWorkResultUnsettledError(RuntimeError):
    """A result-shaped outcome could not be proven safe while teardown was active."""


class LiveWorkOwnershipError(RuntimeError):
    """Confirmed ownership was lost or could no longer be established safely."""


class LiveWorkAckUnprovenError(RuntimeError):
    """Redis may have applied an ACK, but this owner cannot prove its outcome."""


@dataclass
class _LeaseState:
    deadline: float
    failure: str | None = None
    result: dict | None = None
    ack_uncertain: bool = False


async def _within_live_lease[T](state: _LeaseState, attempt: Callable[[], Awaitable[T]]) -> T:
    """Bound native network attempts and retry only inside confirmed ownership."""
    while True:
        remaining = state.deadline - time.monotonic()
        if remaining <= 0:
            raise LiveWorkOwnershipError("lease_uncertainty_exhausted")
        try:
            async with asyncio.timeout(min(LIVE_WORK_LEASE_REFRESH_SECONDS, remaining)):
                result = await attempt()
            if time.monotonic() >= state.deadline:
                raise LiveWorkOwnershipError("lease_uncertainty_exhausted")
            return result
        except (RedisAuthenticationError, RedisAuthorizationError):
            # These native access defects inherit ConnectionError but cannot recover by retry.
            raise
        except (RedisConnectionError, RedisTimeoutError, TimeoutError) as error:
            logger.warning("live_work_redis_uncertain", error_type=type(error).__name__)
            remaining = state.deadline - time.monotonic()
            if remaining <= 0:
                raise LiveWorkOwnershipError("lease_uncertainty_exhausted") from error
            await asyncio.sleep(min(LIVE_WORK_LEASE_REFRESH_SECONDS, remaining))


async def _record_failure(redis: RedisStreamClient, project_id: str, reason: str) -> None:
    try:
        async with asyncio.timeout(LIVE_WORK_LEASE_REFRESH_SECONDS):
            await _mark_live_work_failure(redis, project_id, reason)
    except (Exception, asyncio.CancelledError) as error:
        logger.error(
            "live_work_failure_marker_write_failed",
            project_id=project_id,
            reason=reason,
            error_type=type(error).__name__,
        )
        if isinstance(error, asyncio.CancelledError):
            raise


def live_work_cancel_key(project_id: str) -> str:
    return f"live:work:cancelled:{project_id}"


def live_work_leases_key(project_id: str) -> str:
    return f"live:work:leases:{project_id}"


def live_work_failure_key(project_id: str) -> str:
    return f"live:work:failed:{project_id}"


async def _mark_live_work_failure(redis: RedisStreamClient, project_id: str, reason: str) -> None:
    """Leave a cleanup-visible fence when a cancelled stream entry cannot settle."""
    await redis.redis.set(live_work_failure_key(project_id), reason, ex=LIVE_WORK_LEASE_SECONDS * 2)


def _live_work_result_status(result: dict) -> str | None:
    status = result.get("status")
    if isinstance(status, str):
        return status

    deployment_result = result.get("deployment_result")
    if isinstance(deployment_result, dict):
        deployment_status = deployment_result.get("status")
        if isinstance(deployment_status, str):
            return deployment_status

    if result.get("errors"):
        return "failed"
    return None


def live_work_settled(result: dict) -> dict:
    """Mark a consumer result as safe to ACK even if teardown is active."""
    return {**result, LIVE_WORK_SETTLED_KEY: True}


def live_work_unsettled(result: dict) -> dict:
    """Mark a consumer result as unsafe to ACK while teardown is active."""
    return {**result, LIVE_WORK_SETTLED_KEY: False}


def _live_work_result_is_settled(result: dict) -> bool:
    settled = result.get(LIVE_WORK_SETTLED_KEY)
    if isinstance(settled, bool):
        return settled
    return False


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
        local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
        if score == false then return 0 end
        local now = redis.call('TIME')
        local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
        if tonumber(score) <= now_ms then return 0 end
        local expires = now_ms + ARGV[2] * 1000
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


async def live_work_active(redis: RedisStreamClient, project_id: str) -> bool:
    """Report whether some worker still holds an unexpired lease on this project.

    The lease ZSET is the existing liveness contract: every running job adds a
    token scored with its expiry and refreshes it every
    ``LIVE_WORK_LEASE_REFRESH_SECONDS``. Expiry is compared against Redis server
    time, not the caller's clock, so two consumers on different hosts agree.

    Used to decide whether a reclaimed PEL entry may be taken over. A crashed
    worker stops refreshing and its lease falls out of the live window within
    ``LIVE_WORK_LEASE_SECONDS``; a working one keeps it, and its entry stays
    where it is.
    """
    live = await redis.redis.eval(
        """
        local now = redis.call('TIME')
        local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
        redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
        return redis.call('ZCARD', KEYS[1])
        """,
        1,
        live_work_leases_key(project_id),
    )
    return int(live) > 0


async def _cancel_on_live_teardown(
    redis: RedisStreamClient,
    project_id: str,
    token: str,
    owner: asyncio.Task[object],
    state: _LeaseState,
) -> None:
    try:
        while True:
            await asyncio.sleep(
                min(LIVE_WORK_LEASE_REFRESH_SECONDS, max(0, state.deadline - time.monotonic()))
            )
            if await _confirm_live_work(redis, project_id, token, state):
                state.failure = "teardown"
                owner.cancel()
                return
    except Exception as error:
        state.failure = (
            str(error) if isinstance(error, LiveWorkOwnershipError) else "watchdog_failed"
        )
        logger.error(
            "live_work_watchdog_failed",
            project_id=project_id,
            reason=state.failure,
            error_type=type(error).__name__,
        )
        owner.cancel()


async def _confirm_live_work(
    redis: RedisStreamClient,
    project_id: str,
    token: str,
    state: _LeaseState,
) -> bool:
    """Confirm teardown or renew ownership, never extending an uncertain deadline."""
    started = 0.0

    async def attempt() -> bool:
        nonlocal started
        started = time.monotonic()
        if await redis.redis.exists(live_work_cancel_key(project_id)):
            return True
        if not await _refresh_live_work_lease(redis, project_id, token):
            raise LiveWorkOwnershipError("lease_lost")
        return False

    teardown = await _within_live_lease(state, attempt)
    if not teardown:
        state.deadline = started + LIVE_WORK_LEASE_SECONDS
    return teardown


async def _stop_watch(watch: asyncio.Task) -> None:
    owner = asyncio.current_task()
    assert owner is not None
    cancellations = owner.cancelling()
    watch.cancel()
    try:
        await asyncio.shield(watch)
    except asyncio.CancelledError:
        # Join the cancelled child, but retain a new cancellation of its owner.
        # Shielding prevents that second cancellation from interrupting the
        # child's cleanup while we distinguish the two sources.
        with suppress(asyncio.CancelledError):
            await watch
        if owner.cancelling() > cancellations:
            raise


async def _commit_live_work(
    redis: RedisStreamClient,
    project_id: str,
    token: str,
    state: _LeaseState,
    queue: str,
    group: str,
    message_id: str,
    *,
    cancelled: bool = False,
) -> bool:
    """Validate terminal authority and XACK in one server decision on every retry."""
    reply: int | None = None

    async def attempt() -> int:
        nonlocal reply
        if state.failure and state.failure != "teardown":
            raise LiveWorkOwnershipError(state.failure)
        try:
            reply = await redis.redis.eval(
                """
                local teardown = redis.call('EXISTS', KEYS[1]) == 1
                if (teardown or ARGV[2] == '1') and ARGV[3] == '1' and ARGV[4] ~= '1' then
                    return -1
                end
                local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
                if score == false then return -2 end
                local now = redis.call('TIME')
                local now_ms = now[1] * 1000 + math.floor(now[2] / 1000)
                if tonumber(score) <= now_ms then return -2 end
                if ARGV[5] == '1' and not teardown then return -3 end
                return redis.call('XACK', KEYS[3], ARGV[6], ARGV[7])
                """,
                3,
                live_work_cancel_key(project_id),
                live_work_leases_key(project_id),
                queue,
                token,
                int(state.failure == "teardown"),
                int(state.result is not None),
                int(state.result is not None and _live_work_result_is_settled(state.result)),
                int(cancelled),
                group,
                message_id,
            )
            return reply
        except (RedisAuthenticationError, RedisAuthorizationError):
            raise
        except (asyncio.CancelledError, RedisConnectionError, RedisTimeoutError, TimeoutError):
            # A disconnect/timeout/cancellation cannot tell whether the script
            # committed before the reply disappeared. Never infer settlement.
            state.ack_uncertain = True
            raise

    try:
        outcome = await _within_live_lease(state, attempt)
        if outcome == _COMMIT_UNSETTLED:
            assert state.result is not None
            status = _live_work_result_status(state.result) or "unknown"
            raise LiveWorkResultUnsettledError(
                f"live work returned unsettled result during teardown: {status}"
            )
        if outcome == _COMMIT_LEASE_LOST:
            raise LiveWorkOwnershipError("lease_lost")
        if outcome == _COMMIT_UNFENCED:
            await _record_failure(redis, project_id, "cancel_settlement_failed")
            return False
        if outcome != 1:
            raise LiveWorkAckUnprovenError("live work ACK outcome unproven")
    except LiveWorkResultUnsettledError:
        state.failure = "cancel_settlement_failed"
        await _record_failure(redis, project_id, "cancel_settlement_failed")
        raise
    except LiveWorkOwnershipError as error:
        state.failure = str(error)
        if reply == 1:
            logger.error(
                "live_work_ack_reply_after_deadline", project_id=project_id, entry_id=message_id
            )
        await _record_failure(redis, project_id, str(error))
        raise
    except LiveWorkAckUnprovenError:
        state.failure = "ack_uncertain"
        await _record_failure(redis, project_id, "ack_uncertain")
        raise
    except Exception:
        state.failure = "ack_failed"
        await _record_failure(redis, project_id, "ack_failed")
        raise
    finally:
        if state.ack_uncertain:
            logger.warning("live_work_ack_uncertain", project_id=project_id, entry_id=message_id)
    if cancelled:
        logger.info("live_teardown_active_job_acked", entry_id=message_id)
    return True


async def _record_process_failure(
    redis: RedisStreamClient,
    project_id: str,
    state: _LeaseState,
) -> None:
    # Preserve the process exception even if Redis cannot expose teardown.
    try:
        async with asyncio.timeout(LIVE_WORK_LEASE_REFRESH_SECONDS):
            teardown = await redis.redis.exists(live_work_cancel_key(project_id))
    except Exception:
        teardown = True
    if teardown or state.failure:
        await _record_failure(redis, project_id, "cancel_settlement_failed")


async def _finish_live_work_safely(redis: RedisStreamClient, project_id: str, lease: str) -> None:
    try:
        async with asyncio.timeout(LIVE_WORK_LEASE_REFRESH_SECONDS):
            await _finish_live_work(redis, project_id, lease)
    except Exception as error:
        logger.error(
            "live_work_lease_cleanup_failed", project_id=project_id, error_type=type(error).__name__
        )


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

    started = time.monotonic()
    async with asyncio.timeout(LIVE_WORK_LEASE_REFRESH_SECONDS):
        lease = await _begin_live_work(redis, project_id)
    if lease is None:
        await redis.ack(queue, group, message_id)
        logger.info("live_teardown_job_acked", entry_id=message_id)
        return None

    state = _LeaseState(started + LIVE_WORK_LEASE_SECONDS)
    owner = asyncio.current_task()
    assert owner is not None
    cancellation_watch = asyncio.create_task(
        _cancel_on_live_teardown(redis, project_id, lease, owner, state),
        name=f"live-work-watch:{project_id}",
    )
    primary_failed = False
    try:
        result = await process()
        # Store before the next await: cancellation while stopping the watcher
        # or settling Redis must never turn a returned result into unwound work.
        state.result = result
        await _stop_watch(cancellation_watch)
        await _commit_live_work(redis, project_id, lease, state, queue, group, message_id)
        return result
    except asyncio.CancelledError:
        primary_failed = True
        with suppress(asyncio.CancelledError):
            await _stop_watch(cancellation_watch)
        try:
            # Shutdown settlement gets the existing network-attempt bound as
            # well as the lease bound; an invisible fence cannot delay unwind
            # for an entire remaining lease.
            async with asyncio.timeout(LIVE_WORK_LEASE_REFRESH_SECONDS):
                settled = await _commit_live_work(
                    redis, project_id, lease, state, queue, group, message_id, cancelled=True
                )
        except TimeoutError:
            await _record_failure(
                redis,
                project_id,
                "ack_uncertain" if state.ack_uncertain else "cancel_settlement_failed",
            )
            raise asyncio.CancelledError from None
        except Exception:
            # Terminal validation/failure marking has completed; preserve the
            # owner's cancellation instead of replacing it with a Redis error.
            raise asyncio.CancelledError from None
        if not settled:
            raise
        return None
    except WorkflowCancellationUnprovenError:
        # An external GitHub Actions run may still be live. This is fail-closed
        # regardless of which teardown key is set: never ACK, always fence cleanup.
        primary_failed = True
        state.failure = "workflow_cancellation_unproven"
        with suppress(asyncio.CancelledError):
            await _stop_watch(cancellation_watch)
        with suppress(asyncio.CancelledError):
            await _record_failure(redis, project_id, "workflow_cancellation_unproven")
        raise
    except (LiveWorkResultUnsettledError, LiveWorkOwnershipError, LiveWorkAckUnprovenError):
        primary_failed = True
        raise
    except Exception:
        primary_failed = True
        with suppress(asyncio.CancelledError):
            await _stop_watch(cancellation_watch)
        if state.failure != "ack_failed":
            with suppress(asyncio.CancelledError):
                await _record_process_failure(redis, project_id, state)
        raise
    finally:
        # A new cleanup cancellation must not replace an existing primary failure.
        with suppress(asyncio.CancelledError) if primary_failed else nullcontext():
            try:
                await _stop_watch(cancellation_watch)
            finally:
                await _finish_live_work_safely(redis, project_id, lease)
