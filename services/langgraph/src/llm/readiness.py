"""The startup `llm_channel_ready` record: can each channel of an agent's chain answer?

Logged once per channel when an agent's consumer starts, so a deploy readback
reads from the log which channels will serve the agent and which will fail, and
why, before any user or story reaches it. The probe checks what a call checks
before it starts a CLI — credential, profile, child user, binary — and asks the
CLI for its version in a throwaway HOME. It never calls a model and never runs a
CLI against a profile, so it cannot spend a subscription or refresh a token. No
field carries a secret: the reason is a channel failure's bounded, redacted reason.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from shared.contracts.dto.llm_channel import LLMChannel

from .chain import ChannelChainModel, ChannelSlot
from .cli_turn import CliTurnChatModel
from .errors import ChannelFailure

logger = structlog.get_logger(__name__)

READY = "ready"


@dataclass(frozen=True)
class ChannelReadiness:
    """One channel's readiness: `ready`, or the failure class a call would hit first."""

    channel: LLMChannel
    status: str
    reason: str | None
    cli_version: str | None


async def _probe(slot: ChannelSlot) -> ChannelReadiness:
    failure: ChannelFailure | None = slot.unavailable
    version = None
    if failure is None and isinstance(slot.chat_model, CliTurnChatModel):
        failure, version = await slot.chat_model.readiness()
    if failure is None:
        return ChannelReadiness(slot.channel, READY, None, version)
    return ChannelReadiness(slot.channel, failure.failure_class.value, failure.reason, version)


async def log_channel_readiness(llm: ChannelChainModel) -> list[ChannelReadiness]:
    """Probe and log every channel of one agent's chain, in chain order."""
    results = []
    for position, slot in enumerate(llm.slots, start=1):
        readiness = await _probe(slot)
        log = logger.info if readiness.status == READY else logger.warning
        log(
            "llm_channel_ready",
            agent=llm.agent,
            channel=readiness.channel.value,
            model=slot.model_name,
            position=position,
            status=readiness.status,
            reason=readiness.reason,
            cli_version=readiness.cli_version,
            timeout_s=slot.timeout_seconds,
        )
        results.append(readiness)
    return results
