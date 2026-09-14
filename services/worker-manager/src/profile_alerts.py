"""Deduplicated administrator alerts for host-session profiles that need attention.

The diagnostics publisher is the single reconciler. Each executor has at most one
Redis alert episode: an alertable observation opens it and attempts delivery at
once, failed/partial/unaddressable delivery stays owed with bounded backoff, full
delivery settles it, and a later healthy observation deletes only that executor's
episode so a future regression alerts again. Losing Redis can repeat an alert; it
never changes the published diagnostic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import secrets

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import WatchError
import structlog

from shared.contracts.dto.executor_diagnostics import (
    ExecutorDiagnosticSnapshot,
    ExecutorProfileAlertEpisode,
    ExecutorProfileAlertOutcome,
    ExecutorProfileAlertState,
    ExecutorProfileCondition,
    ExecutorProfileObservation,
    executor_profile_alert_key,
    safe_executor_diagnostic_reason,
)
from shared.contracts.vocab import AgentType
from shared.notifications import AdminDeliveryResult
from shared.redis import decode_redis_value

logger = structlog.get_logger()

#: First retry after an unsettled delivery; doubles per attempt up to the cap.
ALERT_RETRY_BASE = timedelta(seconds=60)
ALERT_RETRY_MAX = timedelta(hours=1)
#: Covers one users-API read plus every Telegram send of a single delivery.
ALERT_LOCK_TTL_MS = 120_000

DeliverToAdmins = Callable[..., Awaitable[AdminDeliveryResult]]


def alert_retry_delay(attempts: int) -> timedelta:
    """Bounded exponential backoff after `attempts` unsettled deliveries."""
    return min(ALERT_RETRY_BASE * (2 ** min(max(attempts - 1, 0), 12)), ALERT_RETRY_MAX)


def alert_message(episode: ExecutorProfileAlertEpisode) -> str:
    """Executor, fixed safe reason and expiry instant only — never credential detail."""
    name = "Claude" if episode.executor is AgentType.CLAUDE else "Codex"
    text = (
        f"{name} executor host-session profile needs attention: "
        f"{safe_executor_diagnostic_reason(episode.reason_code)}"
    )
    if episode.refresh_expires_at is not None:
        expiry = episode.refresh_expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        text += f" Refresh credential expiry: {expiry}."
    return text


class ExecutorProfileAlerts:
    """Reconcile one credential-safe alert episode per host-session executor."""

    def __init__(self, redis: Redis, deliver: DeliverToAdmins):
        self.redis = redis
        self.deliver = deliver

    async def reconcile(self, snapshot: ExecutorDiagnosticSnapshot) -> None:
        """Reconcile every observed profile. Never raises into the publisher."""
        for diagnostic in snapshot.diagnostics:
            if diagnostic.profile is None:
                continue
            try:
                await self._reconcile_executor(
                    diagnostic.executor, diagnostic.profile, snapshot.observed_at
                )
            except Exception as exc:  # noqa: BLE001 — alerting must not stop diagnostics
                logger.warning(
                    "executor_profile_alert_reconcile_failed",
                    executor=diagnostic.executor.value,
                    error_type=type(exc).__name__,
                )

    async def _reconcile_executor(
        self, executor: AgentType, profile: ExecutorProfileObservation, now: datetime
    ) -> None:
        key = executor_profile_alert_key(executor)
        lock_key = f"{key}:lock"
        token = secrets.token_urlsafe(16)
        if not await self.redis.set(lock_key, token, nx=True, px=ALERT_LOCK_TTL_MS):
            logger.info("executor_profile_alert_reconcile_busy", executor=executor.value)
            return
        try:
            episode = await self._read(key, executor)
            if profile.condition is ExecutorProfileCondition.HEALTHY:
                if episode is not None:
                    await self.redis.delete(key)
                    logger.info(
                        "executor_profile_alert_resolved",
                        executor=executor.value,
                        episode_id=episode.episode_id,
                    )
                return
            if (
                episode is None
                or episode.condition is not profile.condition
                or episode.refresh_expires_at != profile.refresh_expires_at
            ):
                episode = ExecutorProfileAlertEpisode(
                    executor=executor,
                    episode_id=secrets.token_urlsafe(18),
                    condition=profile.condition,
                    refresh_expires_at=profile.refresh_expires_at,
                    opened_at=now,
                    state=ExecutorProfileAlertState.OWED,
                    attempts=0,
                    next_attempt_at=now,
                )
                # Persist before sending so a restart or a later tick joins it.
                await self.redis.set(key, episode.model_dump_json())
                logger.info(
                    "executor_profile_alert_opened",
                    executor=executor.value,
                    episode_id=episode.episode_id,
                    condition=episode.condition.value,
                )
            if episode.state is ExecutorProfileAlertState.SETTLED:
                return
            assert episode.next_attempt_at is not None  # noqa: S101 - owed episodes carry one
            if now < episode.next_attempt_at:
                return
            outcome = await self._deliver(episode)
            attempts = episode.attempts + 1
            settled = outcome is ExecutorProfileAlertOutcome.DELIVERED
            episode = ExecutorProfileAlertEpisode.model_validate(
                {
                    **episode.model_dump(),
                    "attempts": attempts,
                    "last_attempt_at": now,
                    "last_outcome": outcome,
                    "state": (
                        ExecutorProfileAlertState.SETTLED
                        if settled
                        else ExecutorProfileAlertState.OWED
                    ),
                    "next_attempt_at": None if settled else now + alert_retry_delay(attempts),
                }
            )
            await self.redis.set(key, episode.model_dump_json())
            logger.info(
                "executor_profile_alert_delivery_recorded",
                executor=executor.value,
                episode_id=episode.episode_id,
                outcome=outcome.value,
                attempts=attempts,
            )
        finally:
            await self._release(lock_key, token)

    async def _read(self, key: str, executor: AgentType) -> ExecutorProfileAlertEpisode | None:
        raw = await self.redis.get(key)
        if raw is None:
            return None
        try:
            episode = ExecutorProfileAlertEpisode.model_validate_json(decode_redis_value(raw))
        except ValidationError:
            logger.warning("executor_profile_alert_episode_invalid", executor=executor.value)
            return None
        if episode.executor is not executor:
            logger.warning("executor_profile_alert_episode_invalid", executor=executor.value)
            return None
        return episode

    async def _deliver(self, episode: ExecutorProfileAlertEpisode) -> ExecutorProfileAlertOutcome:
        level = (
            "warning" if episode.condition is ExecutorProfileCondition.REFRESH_EXPIRING else "error"
        )
        try:
            result = await self.deliver(alert_message(episode), level=level)
        except Exception as exc:  # noqa: BLE001 — a raised delivery is an owed failure
            logger.warning(
                "executor_profile_alert_delivery_failed",
                executor=episode.executor.value,
                episode_id=episode.episode_id,
                error_type=type(exc).__name__,
            )
            return ExecutorProfileAlertOutcome.FAILED
        return ExecutorProfileAlertOutcome(result.status.value)

    async def _release(self, lock_key: str, token: str) -> None:
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                await pipe.watch(lock_key)
                if decode_redis_value(await pipe.get(lock_key)) == token:
                    pipe.multi()
                    pipe.delete(lock_key)
                    await pipe.execute()
                else:
                    await pipe.unwatch()
        except WatchError:
            return
        except Exception as exc:  # noqa: BLE001 — the lock expires on its own
            logger.warning("executor_profile_alert_unlock_failed", error_type=type(exc).__name__)
