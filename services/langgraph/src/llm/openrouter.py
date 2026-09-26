"""The openrouter channel: the existing `ChatOpenAI` configuration, unchanged.

This is the only module in langgraph that constructs `ChatOpenAI` or reads the
OpenRouter env (`*_LLM_MODEL`, `*_LLM_BASE_URL`, `*_LLM_API_KEY`,
`SUMMARIZATION_MODEL`). A missing value is this channel's missing-credential
failure, not a reason for the agent to refuse to run: the chain moves on.
"""

from __future__ import annotations

from typing import Any

from langchain_openai import ChatOpenAI
import openai

from shared.contracts.dto.llm_channel import LLMChannel, LLMChannelConfig

from ..config.agent_llm_env import AGENT_LLM_ENV, missing_llm_env
from .chain import DEFAULT_CHANNEL_TIMEOUT_SECONDS, ChannelSlot
from .errors import ChannelFailure, ChannelFailureClass, classify_error_text
from .vocab import LLMAgent

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
            return ChannelFailure(ChannelFailureClass.QUOTA_EXHAUSTED, reason)
        failure_class = _STATUS_CLASSES.get(status, ChannelFailureClass.SERVER_ERROR)
        return ChannelFailure(failure_class, reason)
    return None
