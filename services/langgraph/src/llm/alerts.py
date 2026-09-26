"""Operator alerts about the money and capacity of the LLM channels.

Every alert decision the channel chain makes lives here, and so does the one
delivery path the OpenRouter balance check (`openrouter.py`) uses:

- ``payment_required``: a channel of any agent refused a call with a 402. One
  alert per channel per re-alert window, whichever agent hit it.
- ``subscriptions_down``: one call failed both ``codex`` and ``claude`` and was
  answered by ``openrouter``. One alert per agent per re-alert window.
- ``openrouter_low_balance``: the balance check read a balance below the
  threshold. Re-armed when the balance is back above it.

Dedup is one Redis key per ``(kind, subject)`` — ``llm:alert:<kind>:<subject>``
— shared by every process that runs a chain (langgraph and architect), with the
re-alert window (``llm.alert_realert_window_hours``) as its TTL. The key is set
only after at least one administrator accepted the alert: a lost alert is worse
than a duplicate, so an unreadable key sends anyway and two processes racing on
the same episode may both send.

Alerting is best-effort. The chain only schedules an alert as a background task
bounded by ``ALERT_DEADLINE_SECONDS``; a delivery, Redis or config failure is
logged and never reaches the model call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from enum import StrEnum
from functools import cache
import math
from typing import Any

from redis.asyncio import Redis
import structlog

from shared.config_store import ConfigStore
from shared.contracts.dto.llm_channel import LLMChannel
from shared.notifications import AdminDeliveryResult, deliver_to_admins

from .errors import ChannelAttempt, ChannelFailureClass

logger = structlog.get_logger(__name__)

REALERT_WINDOW_KEY = "llm.alert_realert_window_hours"
DEFAULT_REALERT_WINDOW_HOURS = 6.0

#: One users-API read plus every Telegram send of one delivery, and the Redis
#: round trips around it. Past this the alert is abandoned as failed.
ALERT_DEADLINE_SECONDS = 30.0

#: System config is read far less often than a model is called.
_CONFIG_CACHE_TTL_SECONDS = 300

_MISSING = object()

Deliver = Callable[..., Awaitable[AdminDeliveryResult]]


class LLMAlertKind(StrEnum):
    """What an alert is about; with its subject it names one dedup key."""

    PAYMENT_REQUIRED = "payment_required"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    SUBSCRIPTIONS_DOWN = "subscriptions_down"
    OPENROUTER_LOW_BALANCE = "openrouter_low_balance"


#: The provider refusals an alert names, by the failure class that reported them.
REFUSAL_KINDS = {
    ChannelFailureClass.PAYMENT_REQUIRED: LLMAlertKind.PAYMENT_REQUIRED,
    ChannelFailureClass.UNAUTHORIZED: LLMAlertKind.UNAUTHORIZED,
    ChannelFailureClass.FORBIDDEN: LLMAlertKind.FORBIDDEN,
}


class AlertOutcome(StrEnum):
    SENT = "sent"
    DEDUPLICATED = "deduplicated"
    FAILED = "failed"


def alert_key(kind: LLMAlertKind, subject: str) -> str:
    return f"llm:alert:{kind.value}:{subject}"


_SUBSCRIPTION_CHANNELS = frozenset({LLMChannel.CODEX, LLMChannel.CLAUDE})


def subscriptions_down(attempts: Sequence[ChannelAttempt]) -> bool:
    """Whether this call already failed on both subscription channels."""
    return _SUBSCRIPTION_CHANNELS <= {attempt.channel for attempt in attempts}


@cache
def system_config(api_base_url: str) -> ConfigStore:
    """One `ConfigStore` per process for the LLM alert keys."""
    return ConfigStore(api_base_url, cache_ttl=_CONFIG_CACHE_TTL_SECONDS)


def config_number(config: Any, key: str, default: float, *, minimum: float = 0.0) -> float:
    """A positive number from system config, or ``default`` with a warning.

    A missing key, an unreadable config API or a value that is not a number
    above ``minimum`` never stops the caller: alerting and the PO carry on with
    the documented default.
    """
    try:
        value = config.get(key, _MISSING)
    except Exception as exc:  # noqa: BLE001 - a config outage must not stop alerting
        logger.warning(
            "llm_alert_config_unreadable", key=key, default=default, error_type=type(exc).__name__
        )
        return default
    if value is _MISSING:
        logger.warning("llm_alert_config_missing", key=key, default=default)
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number) or number <= minimum:
        logger.warning("llm_alert_config_invalid", key=key, default=default)
        return default
    return number


@cache
def _redis_client(redis_url: str) -> Redis:
    """One lazily connecting Redis client per process for the alert keys."""
    return Redis.from_url(redis_url)


_pending: set[asyncio.Task] = set()
_in_flight: set[str] = set()


class LLMAlerts:
    """Deduplicated operator alerts for the LLM channels of one process."""

    def __init__(
        self,
        redis: Callable[[], Redis],
        config: Callable[[], Any],
        *,
        deliver: Deliver = deliver_to_admins,
    ) -> None:
        """``redis`` and ``config`` are resolved on first use, inside the alert path,
        so building a chain never connects anywhere and a bad source is only a
        logged alert failure."""
        self._redis_source = redis
        self._config = config
        self._deliver = deliver

    @classmethod
    def from_settings(cls, settings: Any, redis: Redis | None = None) -> LLMAlerts:
        """Alerts over the process's Redis (``redis`` if given) and its system config."""
        return cls(
            (lambda: redis) if redis is not None else (lambda: _redis_client(settings.redis_url)),
            lambda: system_config(settings.api_base_url),
        )

    @property
    def _redis(self) -> Redis:
        return self._redis_source()

    # --- what the chain reports ---------------------------------------------

    def channel_failed(self, agent: str, attempt: ChannelAttempt) -> None:
        """Schedule the payment alert for a 402; every other failure is not alerted."""
        if attempt.failure_class is not ChannelFailureClass.PAYMENT_REQUIRED:
            return
        self._schedule(
            self.provider_refused(attempt.channel, agent, attempt.failure_class, attempt.reason)
        )

    def call_answered(
        self, agent: str, channel: LLMChannel, attempts: Sequence[ChannelAttempt]
    ) -> None:
        """Schedule the degraded-mode alert when OpenRouter answered after both subscriptions."""
        if channel is not LLMChannel.OPENROUTER or not subscriptions_down(attempts):
            return
        classes = ", ".join(
            f"{attempt.channel.value}={attempt.failure_class.value}"
            for attempt in attempts
            if attempt.channel in _SUBSCRIPTION_CHANNELS
        )
        message = (
            f"Subscription channels down, {agent} running on OpenRouter ({classes}). "
            "OpenRouter is paid per token; restore a subscription channel to end this."
        )
        self._schedule(
            self.alert(LLMAlertKind.SUBSCRIPTIONS_DOWN, agent, message, level="error", agent=agent)
        )

    # --- delivery -----------------------------------------------------------

    async def provider_refused(
        self,
        channel: LLMChannel,
        agent: str,
        failure_class: ChannelFailureClass,
        reason: str,
    ) -> AlertOutcome:
        """Alert that a provider refused a channel (402, or 401/403 on the balance read)."""
        kind = REFUSAL_KINDS[failure_class]
        message = (
            f"LLM channel {channel.value} refused {agent} ({failure_class.value}): {reason}. "
            f"Check the {channel.value} account billing and credentials."
        )
        return await self.alert(
            kind, channel.value, message, level="error", agent=agent, channel=channel.value
        )

    async def alert(
        self, kind: LLMAlertKind, subject: str, message: str, *, level: str, **context: str
    ) -> AlertOutcome:
        """Send one alert unless its key is set; never raises and never outlives the deadline."""
        key = alert_key(kind, subject)
        if key in _in_flight:
            return AlertOutcome.DEDUPLICATED
        _in_flight.add(key)
        try:
            async with asyncio.timeout(ALERT_DEADLINE_SECONDS):
                return await self._alert(key, kind, subject, message, level, context)
        except Exception as exc:  # noqa: BLE001 - alerting never fails its caller
            logger.error(
                "llm_alert_failed",
                kind=kind.value,
                subject=subject,
                error_type=type(exc).__name__,
                **context,
            )
            return AlertOutcome.FAILED
        finally:
            _in_flight.discard(key)

    async def _alert(
        self,
        key: str,
        kind: LLMAlertKind,
        subject: str,
        message: str,
        level: str,
        context: dict[str, str],
    ) -> AlertOutcome:
        try:
            if await self._redis.exists(key):
                logger.debug("llm_alert_deduplicated", kind=kind.value, subject=subject)
                return AlertOutcome.DEDUPLICATED
        except Exception as exc:  # noqa: BLE001 - an unreadable key sends anyway
            logger.warning(
                "llm_alert_dedup_unreadable",
                kind=kind.value,
                subject=subject,
                error_type=type(exc).__name__,
            )
        result = await self._deliver(message, level=level)
        if result.succeeded == 0:
            logger.error(
                "llm_alert_failed",
                kind=kind.value,
                subject=subject,
                delivery=result.status.value,
                **context,
            )
            return AlertOutcome.FAILED
        window_hours = await self.config_number(REALERT_WINDOW_KEY, DEFAULT_REALERT_WINDOW_HOURS)
        try:
            await self._redis.set(key, "1", ex=max(1, round(window_hours * 3600)))
        except Exception as exc:  # noqa: BLE001 - the alert went out; a repeat is acceptable
            logger.warning(
                "llm_alert_dedup_unrecorded",
                kind=kind.value,
                subject=subject,
                error_type=type(exc).__name__,
            )
        logger.info(
            "llm_alert_sent",
            kind=kind.value,
            subject=subject,
            delivery=result.status.value,
            realert_window_hours=window_hours,
            **context,
        )
        return AlertOutcome.SENT

    async def rearm(self, kind: LLMAlertKind, subject: str) -> None:
        """Forget a sent alert so the next episode alerts again at once."""
        try:
            if await self._redis.delete(alert_key(kind, subject)):
                logger.info("llm_alert_rearmed", kind=kind.value, subject=subject)
        except Exception as exc:  # noqa: BLE001 - it re-arms on its own when the key expires
            logger.warning(
                "llm_alert_rearm_failed",
                kind=kind.value,
                subject=subject,
                error_type=type(exc).__name__,
            )

    async def config_number(self, key: str, default: float) -> float:
        """A system-config number read off the event loop; see `config_number`."""
        try:
            config = self._config()
        except Exception as exc:  # noqa: BLE001 - no config source means the default
            logger.warning(
                "llm_alert_config_unreadable",
                key=key,
                default=default,
                error_type=type(exc).__name__,
            )
            return default
        return await asyncio.to_thread(config_number, config, key, default)

    @staticmethod
    def _schedule(alert: Coroutine[Any, Any, AlertOutcome]) -> None:
        try:
            task = asyncio.get_running_loop().create_task(alert)
        except Exception as exc:  # noqa: BLE001 - no loop to alert on, the call goes on
            alert.close()
            logger.error("llm_alert_not_scheduled", error_type=type(exc).__name__)
            return
        _pending.add(task)
        task.add_done_callback(_pending.discard)

    @staticmethod
    async def drain() -> None:
        """Wait for every scheduled alert of this process (tests, shutdown)."""
        while _pending:
            await asyncio.gather(*list(_pending), return_exceptions=True)
