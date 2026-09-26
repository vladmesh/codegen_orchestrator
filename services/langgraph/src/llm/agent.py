"""Which agents answer through a channel chain, and how one is read and built.

The chain of each agent is its `agent_configs` record's `llm_channels`
(`shared.contracts.dto.llm_channel`). No record, or no field, means the default
chain `codex, claude, openrouter`; a stored chain that does not validate is an
`InvalidChannelChainError` — the consumer refuses to start on it rather than
running on a default nobody chose.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from shared.contracts.dto.llm_channel import (
    LLM_CHANNEL_CHAIN_ADAPTER,
    LLMChannel,
    LLMChannelConfig,
    default_llm_channel_chain,
)
from shared.diagnostics import safe_validation_errors

from .chain import DEFAULT_CHANNEL_TIMEOUT_SECONDS, ChannelChainModel, ChannelSlot
from .cli_turn import ClaudeTurnModel, CodexTurnModel
from .errors import InvalidChannelChainError
from .openrouter import openrouter_missing_env, openrouter_slot
from .vocab import LLMAgent


async def load_channel_chain(api: Any, agent: LLMAgent) -> list[LLMChannelConfig]:
    """The agent's configured chain, the default chain, or `InvalidChannelChainError`."""
    try:
        record = await api.get_agent_config(agent.value)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != HTTPStatus.NOT_FOUND:
            raise
        record = None
    stored = None if record is None else record.get("llm_channels")
    if stored is None:
        return default_llm_channel_chain()
    try:
        return LLM_CHANNEL_CHAIN_ADAPTER.validate_python(stored)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'} ({error['type']})"
            for error in safe_validation_errors(exc)
        )
        raise InvalidChannelChainError(agent.value, f"does not validate: {details}") from None


def unconfigured_channel_env(
    agent: LLMAgent, chain: list[LLMChannelConfig], settings: Any
) -> list[str]:
    """Env the agent cannot run without: empty once any channel of its chain is configured.

    A chain with one configured channel runs; every other channel's missing
    credential is only that channel's failure. A chain with none configured
    (no `LLM_CODEX_HOME`, no `CLAUDE_CODE_OAUTH_TOKEN`, no complete OpenRouter
    env) could only ever raise `LLMChannelsExhausted`, so it names all of them.
    """
    missing: list[str] = []
    for entry in chain:
        if entry.channel is LLMChannel.CODEX:
            lacking = [] if settings.llm_codex_home else ["LLM_CODEX_HOME"]
        elif entry.channel is LLMChannel.CLAUDE:
            lacking = [] if settings.claude_code_oauth_token else ["CLAUDE_CODE_OAUTH_TOKEN"]
        else:
            lacking = openrouter_missing_env(agent, settings, model=entry.model)
        if not lacking:
            return []
        missing += lacking
    return list(dict.fromkeys(missing))


def build_agent_llm(
    agent: LLMAgent, chain: list[LLMChannelConfig], settings: Any
) -> ChannelChainModel:
    """The one chat model the agent's graph receives."""
    slots = []
    for entry in chain:
        timeout = entry.timeout_seconds or DEFAULT_CHANNEL_TIMEOUT_SECONDS
        if entry.channel is LLMChannel.OPENROUTER:
            slots.append(openrouter_slot(agent, entry, settings))
        elif entry.channel is LLMChannel.CODEX:
            model = CodexTurnModel(model=entry.model, codex_home=settings.llm_codex_home)
            slots.append(ChannelSlot(entry.channel, entry.model, model, timeout))
        else:
            token = settings.claude_code_oauth_token
            model = ClaudeTurnModel(
                model=entry.model, oauth_token=SecretStr(token) if token else None
            )
            slots.append(ChannelSlot(entry.channel, entry.model, model, timeout))
    return ChannelChainModel(agent=agent.value, slots=slots)
