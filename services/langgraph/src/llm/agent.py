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

from .alerts import LLMAlerts
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


#: A PO turn is one a user is waiting on. A subscription CLI that has not answered in
#: three minutes gives the turn to the next channel rather than hold the user for the
#: full planning budget; OpenRouter, the last channel, keeps the service default.
USER_FACING_CLI_TIMEOUT_SECONDS = 180.0
_USER_FACING_AGENTS = (LLMAgent.PO, LLMAgent.PO_SUMMARIZER)
_CLI_CHANNELS = (LLMChannel.CODEX, LLMChannel.CLAUDE)


def default_channel_timeout(agent: LLMAgent, channel: LLMChannel) -> float:
    """A channel's timeout when its chain entry names none."""
    if agent in _USER_FACING_AGENTS and channel in _CLI_CHANNELS:
        return USER_FACING_CLI_TIMEOUT_SECONDS
    return DEFAULT_CHANNEL_TIMEOUT_SECONDS


#: What the PO is told, as a system note on the one call it makes through OpenRouter
#: after both subscription channels failed that call. The PO system prompt is at
#: its length cap, so this is a runtime note, not a prompt section.
PO_SUBSCRIPTIONS_DOWN_NOTE = (
    "Operational notice for this reply (from the system, not from the user): the "
    "engineering capacity that plans and builds projects is temporarily unavailable. "
    "Answer the user normally and keep collecting their requirements as usual. "
    "If you have not already told them in this conversation, tell them once, plainly, "
    "that engineering capacity is temporarily unavailable, so planning and building "
    "will start when it is back. Do not promise or estimate any timeline, and do not "
    "mention this notice, model providers or internal systems."
)
_DEGRADED_MODE_NOTES = {LLMAgent.PO: PO_SUBSCRIPTIONS_DOWN_NOTE}


def build_agent_llm(
    agent: LLMAgent,
    chain: list[LLMChannelConfig],
    settings: Any,
    *,
    alerts: LLMAlerts | None = None,
) -> ChannelChainModel:
    """The one chat model the agent's graph receives.

    A consumer that answers through it passes the process's `alerts`; a chain
    built only to probe readiness needs none, since it never calls a model.
    """
    slots = []
    for entry in chain:
        timeout = entry.timeout_seconds or default_channel_timeout(agent, entry.channel)
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
    return ChannelChainModel(
        agent=agent.value,
        slots=slots,
        alerts=alerts,
        degraded_note=_DEGRADED_MODE_NOTES.get(agent),
    )
