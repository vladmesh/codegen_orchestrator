"""The ordered LLM channel chain an LLM-backed service agent answers through.

The Architect, the PO and the PO summarizer each read their chain from their
``agent_configs`` record (``llm_channels``). A model call tries the channels in
order and returns the first answer; a channel failure moves the same call to the
next channel. A record without ``llm_channels`` — or no record at all — means
``DEFAULT_LLM_CHANNELS``: the subscription CLIs first, OpenRouter last.

The same annotated type validates the chain where it is written (the API) and
where it is read (langgraph), so a chain the API accepted is a chain the agent
can start with, and a stored chain the agent refuses names why.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter


class LLMChannel(StrEnum):
    """Where one model call can be answered."""

    CODEX = "codex"
    CLAUDE = "claude"
    OPENROUTER = "openrouter"


class LLMChannelConfig(BaseModel):
    """One channel of a chain and the model it asks for.

    ``model`` unset means the channel's default: the CLI's own default model for
    ``codex``/``claude``, the agent's OpenRouter env model for ``openrouter``.
    ``timeout_seconds`` unset means the service default for one model turn.
    """

    model_config = ConfigDict(extra="forbid")

    channel: LLMChannel
    model: str | None = Field(default=None, min_length=1, max_length=200)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)


def _non_empty_without_duplicates(chain: list[LLMChannelConfig]) -> list[LLMChannelConfig]:
    if not chain:
        raise ValueError("an LLM channel chain needs at least one channel")
    names = [entry.channel for entry in chain]
    duplicates = sorted({name.value for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"an LLM channel chain names each channel once: {duplicates}")
    return chain


LLMChannelChain = Annotated[list[LLMChannelConfig], AfterValidator(_non_empty_without_duplicates)]

LLM_CHANNEL_CHAIN_ADAPTER: TypeAdapter[list[LLMChannelConfig]] = TypeAdapter(LLMChannelChain)

#: The owner's order (sprint:1466): Codex subscription, Claude subscription, OpenRouter.
DEFAULT_LLM_CHANNELS: tuple[LLMChannel, ...] = (
    LLMChannel.CODEX,
    LLMChannel.CLAUDE,
    LLMChannel.OPENROUTER,
)


def default_llm_channel_chain() -> list[LLMChannelConfig]:
    """The chain an agent runs on when its configuration names none."""
    return [LLMChannelConfig(channel=channel) for channel in DEFAULT_LLM_CHANNELS]
