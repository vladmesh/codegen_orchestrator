"""What a channel failure is, and the error a chain raises when every channel failed."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from shared.contracts.dto.llm_channel import LLMChannel
from shared.diagnostics import redact_diagnostic

#: A failure reason is a log field, not a transcript.
REASON_LIMIT = 300


class ChannelFailureClass(StrEnum):
    """Why a channel could not answer. Every one moves the call to the next channel."""

    UNAUTHORIZED = "unauthorized"  # HTTP/CLI-reported 401
    PAYMENT_REQUIRED = "payment_required"  # 402
    FORBIDDEN = "forbidden"  # 403
    RATE_LIMITED = "rate_limited"  # 429
    SERVER_ERROR = "server_error"  # 5xx
    UNREACHABLE = "unreachable"  # no connection to the provider at all
    QUOTA_EXHAUSTED = "quota_exhausted"  # subscription usage limit / credits exhausted
    TIMEOUT = "timeout"
    MISSING_CREDENTIAL = "missing_credential"
    BINARY_MISSING = "binary_missing"
    NONZERO_EXIT = "nonzero_exit"
    INVALID_OUTPUT = "invalid_output"


_STATUS = re.compile(r"(?:status|http|error|code)\D{0,12}\b([45]\d\d)\b", re.IGNORECASE)
_QUOTA = re.compile(
    r"usage limit|quota|out of credits|credit balance|insufficient[_ ]credits|"
    r"limit reached|hit your .{0,40}limit",
    re.IGNORECASE,
)
_KEYWORDS: tuple[tuple[re.Pattern[str], ChannelFailureClass], ...] = (
    (
        re.compile(
            r"unauthori[sz]ed|authentication_error|invalid (?:api key|token|bearer)|"
            r"not logged in|log ?in again|token (?:has )?expired",
            re.IGNORECASE,
        ),
        ChannelFailureClass.UNAUTHORIZED,
    ),
    (re.compile(r"payment required", re.IGNORECASE), ChannelFailureClass.PAYMENT_REQUIRED),
    (re.compile(r"forbidden|permission_error", re.IGNORECASE), ChannelFailureClass.FORBIDDEN),
    (
        re.compile(r"rate[ _-]?limit|too many requests", re.IGNORECASE),
        ChannelFailureClass.RATE_LIMITED,
    ),
    (
        re.compile(
            r"overloaded|internal server error|bad gateway|service unavailable|gateway timeout",
            re.IGNORECASE,
        ),
        ChannelFailureClass.SERVER_ERROR,
    ),
)
_STATUS_CLASSES = {
    401: ChannelFailureClass.UNAUTHORIZED,
    402: ChannelFailureClass.PAYMENT_REQUIRED,
    403: ChannelFailureClass.FORBIDDEN,
    429: ChannelFailureClass.RATE_LIMITED,
}


def classify_error_text(text: str) -> ChannelFailureClass:
    """The failure class a CLI or provider reported, from its own error text.

    An exhausted subscription or credit balance is named first: it is reported
    with a 429 or 402 too, and it is the one failure that does not clear on its
    own within minutes. Text naming no known failure is a plain non-zero exit.
    """
    if _QUOTA.search(text):
        return ChannelFailureClass.QUOTA_EXHAUSTED
    status = _STATUS.search(text)
    if status:
        code = int(status.group(1))
        if code in _STATUS_CLASSES:
            return _STATUS_CLASSES[code]
        if code >= 500:  # noqa: PLR2004
            return ChannelFailureClass.SERVER_ERROR
    for pattern, failure_class in _KEYWORDS:
        if pattern.search(text):
            return failure_class
    return ChannelFailureClass.NONZERO_EXIT


def error_text_status(text: str) -> int | None:
    """The HTTP status a CLI or provider error text names, if it names one.

    Independent of `classify_error_text`: a 402 whose text says "insufficient
    credits" is classified `quota_exhausted`, and it is still a 402.
    """
    status = _STATUS.search(text)
    return int(status.group(1)) if status else None


def short_reason(text: object, *, secrets: tuple[str, ...] = ()) -> str:
    """A bounded, redacted, single-line reason safe for a log field."""
    line = " ".join(redact_diagnostic(text, secrets=secrets).split())
    return line if len(line) <= REASON_LIMIT else line[: REASON_LIMIT - 1] + "…"


class ChannelFailure(Exception):  # noqa: N818 - a failure outcome, raised to switch channels
    """One channel could not answer this call; the chain tries the next one.

    ``http_status`` is the provider's HTTP status when the channel knows it — the
    OpenRouter response, or a status the CLI's error output names — else ``None``.
    """

    def __init__(
        self, failure_class: ChannelFailureClass, reason: str, *, http_status: int | None = None
    ) -> None:
        self.failure_class = failure_class
        self.http_status = http_status
        self.reason = short_reason(reason)
        super().__init__(f"{failure_class.value}: {self.reason}")


@dataclass(frozen=True)
class ChannelAttempt:
    """A channel the chain tried and skipped."""

    channel: LLMChannel
    failure_class: ChannelFailureClass
    reason: str
    http_status: int | None = None


class LLMChannelsExhausted(RuntimeError):  # noqa: N818 - named by the card and its consumers
    """Every channel of an agent's chain failed the same model call.

    ``attempts`` names each channel and its failure class, in chain order, for the
    planning-failure state, the operator alert and the PO's emergency message.
    """

    def __init__(self, agent: str, attempts: list[ChannelAttempt]) -> None:
        self.agent = agent
        self.attempts = list(attempts)
        summary = ", ".join(
            f"{attempt.channel.value}={attempt.failure_class.value}" for attempt in self.attempts
        )
        super().__init__(f"every LLM channel of {agent} failed: {summary}")


#: Failures another try minutes later does not clear: a refused payment, a bad
#: or missing credential, an exhausted quota, a CLI that is not installed. An
#: operator has to act before the channel can answer again.
UNRETRIABLE_FAILURE_CLASSES = frozenset(
    {
        ChannelFailureClass.PAYMENT_REQUIRED,
        ChannelFailureClass.UNAUTHORIZED,
        ChannelFailureClass.FORBIDDEN,
        ChannelFailureClass.QUOTA_EXHAUSTED,
        ChannelFailureClass.MISSING_CREDENTIAL,
        ChannelFailureClass.BINARY_MISSING,
    }
)


def retry_cannot_fix(exc: BaseException) -> bool:
    """Whether retrying the call that raised ``exc`` is pointless.

    Only when every channel of the chain failed, and each with a class in
    ``UNRETRIABLE_FAILURE_CLASSES``. One channel that was merely rate-limited,
    overloaded or slow may answer next time, and any other error is treated as
    transient: a retry is bounded, a wrong "never" is not.
    """
    if not isinstance(exc, LLMChannelsExhausted) or not exc.attempts:
        return False
    return all(attempt.failure_class in UNRETRIABLE_FAILURE_CLASSES for attempt in exc.attempts)


class InvalidChannelChainError(RuntimeError):
    """An agent's stored ``llm_channels`` is not a chain it can run on."""

    def __init__(self, agent: str, reason: str) -> None:
        self.agent = agent
        self.reason = reason
        super().__init__(f"invalid_llm_channel_chain: agent_configs[{agent}].llm_channels {reason}")
