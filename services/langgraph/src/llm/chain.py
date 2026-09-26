"""One chat model over an ordered list of LLM channels.

`ChannelChainModel` is what `create_react_agent` and the PO `SummarizationNode`
receive. A call tries the channels in order and returns the first answer. A
`ChannelFailure` — raised by a CLI channel, or classified from a provider
exception by the channel's own classifier — or the channel's timeout moves the
same call to the next channel. Anything else (a bug in our code, cancellation)
propagates at once. When every channel failed, `LLMChannelsExhausted` names
each one and its failure class.

Every answered call logs `llm_channel_used`; every skipped channel logs
`llm_channel_failed`. The answering channel is also in the returned message's
`response_metadata["llm_channel"]`, and in the collector `channel_usage()`
opens, which is how a consumer names the channels one planning attempt used.

A chain given `alerts` reports every failure and every answer to it
(`alerts.py` decides what the operator hears). A chain given a `degraded_note`
appends it, as one system message, to the call it sends to `openrouter` after
`codex` and `claude` both failed that same call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
import structlog

from shared.contracts.dto.llm_channel import LLMChannel

from .alerts import LLMAlerts, subscriptions_down
from .errors import ChannelAttempt, ChannelFailure, ChannelFailureClass, LLMChannelsExhausted

logger = structlog.get_logger(__name__)

#: One model turn of a planning agent can reason for minutes; a dead channel must
#: still give the call back to the next one well before a planning heartbeat matters.
DEFAULT_CHANNEL_TIMEOUT_SECONDS = 600.0

Classifier = Callable[[BaseException], ChannelFailure | None]


def _no_classification(_exc: BaseException) -> ChannelFailure | None:
    return None


@dataclass(frozen=True)
class ChannelSlot:
    """One channel of a chain as the chain calls it.

    ``chat_model`` is ``None`` exactly when ``unavailable`` says why the channel
    cannot be called at all (for example a missing OpenRouter key); the chain
    records that failure and moves on without calling anything.
    """

    channel: LLMChannel
    model_name: str | None
    chat_model: BaseChatModel | None
    timeout_seconds: float = DEFAULT_CHANNEL_TIMEOUT_SECONDS
    classify: Classifier = _no_classification
    unavailable: ChannelFailure | None = None


@dataclass
class ChannelUsage:
    """The channels that answered, and the ones skipped, inside one `channel_usage()`."""

    answered: list[LLMChannel] = field(default_factory=list)
    failed: list[ChannelAttempt] = field(default_factory=list)

    def channels(self) -> list[str]:
        """Distinct answering channels in order of first use."""
        return list(dict.fromkeys(channel.value for channel in self.answered))


_usage: ContextVar[ChannelUsage | None] = ContextVar("llm_channel_usage", default=None)


@contextmanager
def channel_usage() -> Iterator[ChannelUsage]:
    """Collect every chain call made in this context (graph tasks included)."""
    usage = ChannelUsage()
    token = _usage.set(usage)
    try:
        yield usage
    finally:
        _usage.reset(token)


class ChannelChainModel(BaseChatModel):
    """Try each channel in order; the first answer wins."""

    agent: str
    slots: list[ChannelSlot]
    alerts: LLMAlerts | None = None
    degraded_note: str | None = None
    bound_tools: list[Any] | None = None
    bind_tools_kwargs: dict[str, Any] = {}

    @property
    def _llm_type(self) -> str:
        return "llm-channel-chain"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"agent": self.agent, "channels": self.describe()}

    def describe(self) -> list[str]:
        """`channel:model` per slot, in order; `default` is the CLI's own model."""
        return [f"{slot.channel.value}:{slot.model_name or 'default'}" for slot in self.slots]

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ChannelChainModel:
        """The same tools, bound on every channel at call time."""
        extra = dict(kwargs)
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        return self.model_copy(update={"bound_tools": list(tools), "bind_tools_kwargs": extra})

    def _runnable(self, slot: ChannelSlot):
        assert slot.chat_model is not None
        if self.bound_tools is None:
            return slot.chat_model
        return slot.chat_model.bind_tools(self.bound_tools, **self.bind_tools_kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError(
            "ChannelChainModel is async-only: channel timeouts and CLI subprocesses "
            "are driven by the event loop"
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        attempts: list[ChannelAttempt] = []
        usage = _usage.get()
        for position, slot in enumerate(self.slots, start=1):
            started = time.monotonic()
            call_messages = messages
            if (
                self.degraded_note is not None
                and slot.channel is LLMChannel.OPENROUTER
                and subscriptions_down(attempts)
            ):
                call_messages = [*messages, SystemMessage(content=self.degraded_note)]
                logger.info("llm_degraded_note_added", agent=self.agent, position=position)
            try:
                message = await self._call(slot, call_messages, stop, kwargs)
            except ChannelFailure as failure:
                attempt = ChannelAttempt(
                    slot.channel, failure.failure_class, failure.reason, failure.http_status
                )
                attempts.append(attempt)
                if usage is not None:
                    usage.failed.append(attempt)
                logger.warning(
                    "llm_channel_failed",
                    agent=self.agent,
                    channel=slot.channel.value,
                    model=slot.model_name,
                    position=position,
                    failure_class=failure.failure_class.value,
                    http_status=failure.http_status,
                    reason=failure.reason,
                    duration_s=round(time.monotonic() - started, 3),
                )
                if self.alerts is not None:
                    self.alerts.channel_failed(self.agent, attempt)
                continue
            duration = round(time.monotonic() - started, 3)
            logger.info(
                "llm_channel_used",
                agent=self.agent,
                channel=slot.channel.value,
                model=slot.model_name,
                position=position,
                duration_s=duration,
            )
            if usage is not None:
                usage.answered.append(slot.channel)
            if self.alerts is not None:
                self.alerts.call_answered(self.agent, slot.channel, attempts)
            message.response_metadata = {
                **message.response_metadata,
                "llm_channel": slot.channel.value,
                "llm_channel_position": position,
            }
            return ChatResult(generations=[ChatGeneration(message=message)])
        raise LLMChannelsExhausted(self.agent, attempts)

    async def _call(
        self,
        slot: ChannelSlot,
        messages: list[BaseMessage],
        stop: list[str] | None,
        kwargs: dict[str, Any],
    ) -> AIMessage:
        """One channel's answer, or the `ChannelFailure` that skips it."""
        if slot.unavailable is not None:
            raise slot.unavailable
        runnable = self._runnable(slot)
        deadline = asyncio.timeout(slot.timeout_seconds)
        try:
            async with deadline:
                message = await runnable.ainvoke(messages, stop=stop, **kwargs)
        except ChannelFailure:
            raise
        except TimeoutError:
            if deadline.expired():
                raise ChannelFailure(
                    ChannelFailureClass.TIMEOUT, f"no answer within {slot.timeout_seconds:g}s"
                ) from None
            raise
        except Exception as exc:
            failure = slot.classify(exc)
            if failure is None:
                raise
            raise failure from exc
        if not isinstance(message, AIMessage):
            raise TypeError(f"channel {slot.channel.value} returned {type(message).__name__}")
        return message
