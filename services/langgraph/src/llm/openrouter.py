"""The openrouter channel: the existing `ChatOpenAI` configuration, unchanged.

This is the only module in langgraph that constructs `ChatOpenAI` or reads the
OpenRouter env (`*_LLM_MODEL`, `*_LLM_BASE_URL`, `*_LLM_API_KEY`,
`SUMMARIZATION_MODEL`). A missing value is this channel's missing-credential
failure, not a reason for the agent to refuse to run: the chain moves on.

It also owns the scheduled OpenRouter balance check the `langgraph` process
runs (`run_openrouter_balance_check`), since that check reads the same key, or
the optional `OPENROUTER_MANAGEMENT_KEY`, which nothing else reads.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from langchain_openai import ChatOpenAI
import openai
import structlog

from shared.contracts.dto.llm_channel import LLMChannel, LLMChannelConfig

from ..config.agent_llm_env import AGENT_LLM_ENV, missing_llm_env
from .alerts import REFUSAL_KINDS, AlertOutcome, LLMAlertKind, LLMAlerts
from .chain import DEFAULT_CHANNEL_TIMEOUT_SECONDS, ChannelSlot
from .errors import ChannelFailure, ChannelFailureClass, classify_error_text, short_reason
from .vocab import LLMAgent

logger = structlog.get_logger(__name__)

_STATUS_CLASSES = {
    401: ChannelFailureClass.UNAUTHORIZED,
    402: ChannelFailureClass.PAYMENT_REQUIRED,
    403: ChannelFailureClass.FORBIDDEN,
    429: ChannelFailureClass.RATE_LIMITED,
}


def _env_group(agent: LLMAgent) -> str:
    """The `AGENT_LLM_ENV` group; the summarizer rides on the PO's endpoint and key."""
    return "po" if agent is LLMAgent.PO_SUMMARIZER else agent.value


def _default_model(agent: LLMAgent, settings: Any) -> str | None:
    """Today's env model: SUMMARIZATION_MODEL (else PO_LLM_MODEL) for the summarizer."""
    if agent is LLMAgent.PO_SUMMARIZER and settings.summarization_model:
        return settings.summarization_model
    return getattr(settings, AGENT_LLM_ENV[_env_group(agent)][0].lower())


def openrouter_missing_env(
    agent: LLMAgent, settings: Any, *, model: str | None = None
) -> list[str]:
    """Env names the openrouter channel of this agent lacks (a configured model covers one)."""
    model_env = AGENT_LLM_ENV[_env_group(agent)][0]
    missing = missing_llm_env(_env_group(agent), settings)
    if model or _default_model(agent, settings):
        missing = [name for name in missing if name != model_env]
    return missing


def openrouter_slot(agent: LLMAgent, entry: LLMChannelConfig, settings: Any) -> ChannelSlot:
    """The openrouter channel of an agent's chain, or its missing-credential failure."""
    _, base_url_env, key_env = AGENT_LLM_ENV[_env_group(agent)]
    model = entry.model or _default_model(agent, settings)
    timeout = entry.timeout_seconds or DEFAULT_CHANNEL_TIMEOUT_SECONDS
    missing = openrouter_missing_env(agent, settings, model=entry.model)
    if missing:
        return ChannelSlot(
            channel=LLMChannel.OPENROUTER,
            model_name=model,
            chat_model=None,
            timeout_seconds=timeout,
            unavailable=ChannelFailure(
                ChannelFailureClass.MISSING_CREDENTIAL, f"{', '.join(missing)} not set"
            ),
        )
    api_key = getattr(settings, key_env.lower())
    return ChannelSlot(
        channel=LLMChannel.OPENROUTER,
        model_name=model,
        chat_model=ChatOpenAI(
            model=model, base_url=getattr(settings, base_url_env.lower()), api_key=api_key
        ),
        timeout_seconds=timeout,
        classify=lambda exc: classify_openrouter_error(exc, secrets=(api_key,)),
    )


def classify_openrouter_error(
    exc: BaseException, *, secrets: tuple[str, ...] = ()
) -> ChannelFailure | None:
    """A provider failure as a channel failure; anything else is not one (`None`)."""
    if isinstance(exc, openai.APITimeoutError):
        return ChannelFailure(ChannelFailureClass.TIMEOUT, "OpenRouter request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return ChannelFailure(ChannelFailureClass.UNREACHABLE, "OpenRouter is unreachable")
    if not isinstance(exc, openai.APIStatusError):
        return None
    status = exc.status_code
    message = str(exc.message)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    reason = f"HTTP {status}: {message}"
    if status in _STATUS_CLASSES or status >= 500:  # noqa: PLR2004
        if classify_error_text(message) is ChannelFailureClass.QUOTA_EXHAUSTED:
            return ChannelFailure(ChannelFailureClass.QUOTA_EXHAUSTED, reason, http_status=status)
        failure_class = _STATUS_CLASSES.get(status, ChannelFailureClass.SERVER_ERROR)
        return ChannelFailure(failure_class, reason, http_status=status)
    return None


# --- the scheduled balance check ---------------------------------------------

#: `GET <base_url>/credits`, documented at
#: https://openrouter.ai/docs/api-reference/get-credits (checked 2026-09-26):
#: `{"data": {"total_credits": <USD purchased>, "total_usage": <USD used>}}`.
#: The docs mark it "Management key required": the check sends
#: `OPENROUTER_MANAGEMENT_KEY` when it is set, otherwise the PO's inference key,
#: and a 401/402/403 it gets back is alerted like a refused channel.
CREDITS_PATH = "/credits"
BALANCE_THRESHOLD_KEY = "llm.openrouter_balance_alert_usd"
DEFAULT_BALANCE_THRESHOLD_USD = 20.0
BALANCE_INTERVAL_KEY = "llm.openrouter_balance_check_interval_minutes"
DEFAULT_BALANCE_INTERVAL_MINUTES = 30.0
BALANCE_READ_TIMEOUT_SECONDS = 15.0
#: The agent name the balance check's alerts carry.
BALANCE_CHECK_AGENT = "openrouter_balance_check"
_BALANCE_SUBJECT = LLMChannel.OPENROUTER.value
#: The endpoint and inference key the check falls back on are the PO's, which
#: the langgraph process holds.
_BALANCE_ENV_GROUP = "po"
MANAGEMENT_KEY_ENV = "OPENROUTER_MANAGEMENT_KEY"
_REFUSED_READ_FIX = (
    f"The balance read needs an OpenRouter management key: set {MANAGEMENT_KEY_ENV} to a valid one."
)


class BalanceReadError(Exception):
    """The balance could not be read; ``failure`` is set when the provider refused the key."""

    def __init__(self, reason: str, failure: ChannelFailure | None = None) -> None:
        self.reason = reason
        self.failure = failure
        super().__init__(reason)


async def read_openrouter_balance(client: httpx.AsyncClient, base_url: str, api_key: str) -> float:
    """``total_credits - total_usage`` in USD, or `BalanceReadError`."""
    url = base_url.rstrip("/") + CREDITS_PATH
    try:
        response = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
    except httpx.HTTPError as exc:
        raise BalanceReadError(f"request failed: {type(exc).__name__}") from None
    if response.status_code != httpx.codes.OK:
        reason = short_reason(f"HTTP {response.status_code}: {response.text}", secrets=(api_key,))
        refused = _STATUS_CLASSES.get(response.status_code)
        if refused is ChannelFailureClass.RATE_LIMITED:
            refused = None
        failure = (
            ChannelFailure(refused, reason, http_status=response.status_code) if refused else None
        )
        raise BalanceReadError(reason, failure)
    try:
        data = response.json()["data"]
        return float(data["total_credits"]) - float(data["total_usage"])
    except (KeyError, TypeError, ValueError):
        raise BalanceReadError("unexpected response shape") from None


async def check_openrouter_balance(
    alerts: LLMAlerts, client: httpx.AsyncClient, base_url: str, api_key: str
) -> None:
    """Read the balance once and alert, or re-arm the alert. Never raises."""
    try:
        balance = await read_openrouter_balance(client, base_url, api_key)
    except BalanceReadError as exc:
        outcome = None
        if exc.failure is not None:
            payment = exc.failure.failure_class is ChannelFailureClass.PAYMENT_REQUIRED
            outcome = await alerts.provider_refused(
                LLMChannel.OPENROUTER,
                BALANCE_CHECK_AGENT,
                REFUSAL_KINDS[exc.failure.failure_class],
                exc.failure.reason,
                fix=None if payment else _REFUSED_READ_FIX,
            )
        logger.warning(
            "openrouter_balance_read_failed",
            reason=exc.reason,
            failure_class=None if exc.failure is None else exc.failure.failure_class.value,
            alert_outcome=None if outcome is None else outcome.value,
        )
        return
    threshold = await alerts.config_number(BALANCE_THRESHOLD_KEY, DEFAULT_BALANCE_THRESHOLD_USD)
    outcome = None
    if balance < threshold:
        outcome = await alerts.alert(
            LLMAlertKind.OPENROUTER_LOW_BALANCE,
            _BALANCE_SUBJECT,
            f"OpenRouter balance is {balance:.2f} USD, below the {threshold:.2f} USD "
            "alert threshold. Top up OpenRouter credits before the fallback channel stops.",
            level="warning",
        )
    else:
        await alerts.rearm(LLMAlertKind.OPENROUTER_LOW_BALANCE, _BALANCE_SUBJECT)
    logger.info(
        "openrouter_balance",
        balance_usd=round(balance, 2),
        threshold_usd=threshold,
        below_threshold=balance < threshold,
        alert_sent=outcome is AlertOutcome.SENT,
        alert_outcome=None if outcome is None else outcome.value,
    )


async def run_openrouter_balance_check(settings: Any, alerts: LLMAlerts) -> None:
    """Check the OpenRouter balance every N minutes; idle when no key is configured.

    The key is `OPENROUTER_MANAGEMENT_KEY` when set (an empty value is unset),
    otherwise the PO's inference key; the endpoint is always `PO_LLM_BASE_URL`.
    """
    _, base_url_env, key_env = AGENT_LLM_ENV[_BALANCE_ENV_GROUP]
    base_url = getattr(settings, base_url_env.lower())
    management_key = settings.openrouter_management_key
    api_key = management_key or getattr(settings, key_env.lower())
    if not base_url or not api_key:
        logger.info(
            "openrouter_balance_check_idle",
            missing_env=[
                name for name in (base_url_env, key_env) if not getattr(settings, name.lower())
            ],
        )
        return
    logger.info(
        "openrouter_balance_check_started",
        key_source="management" if management_key else "po_inference",
    )
    async with httpx.AsyncClient(timeout=BALANCE_READ_TIMEOUT_SECONDS) as client:
        while True:
            try:
                await check_openrouter_balance(alerts, client, base_url, api_key)
            except Exception as exc:  # noqa: BLE001 - one bad round never ends the schedule
                logger.error("openrouter_balance_check_failed", error_type=type(exc).__name__)
            minutes = await alerts.config_number(
                BALANCE_INTERVAL_KEY, DEFAULT_BALANCE_INTERVAL_MINUTES
            )
            await asyncio.sleep(minutes * 60)
