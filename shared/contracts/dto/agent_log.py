"""One line of an agent run's live log and the writer every producer emits it with.

The worker wrapper writes a coding agent's lines keyed by its engineering or QA attempt;
the langgraph architect writes a planning run's lines keyed by its planning attempt.
"""

from collections.abc import Iterable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
import structlog

from shared.diagnostics import redact_diagnostic

#: The structlog event name every live log line is written under.
AGENT_LOG_LINE_EVENT = "agent_log_line"

#: Upper bound of ``AgentLogLine.text``, applied after redaction.
AGENT_LOG_TEXT_MAX_CHARS = 2000

logger = structlog.get_logger(__name__)


class AgentLogKind(StrEnum):
    """What one live log line reports."""

    STEP = "step"
    INFO = "info"
    CMD = "cmd"
    FILE = "file"
    OK = "ok"
    ERR = "err"


class AgentLogLine(BaseModel):
    """A redacted, bounded line of one attempt's agent run, ordered by ``seq``.

    ``seq`` grows by one per line across every agent process of the attempt.
    ``task_id`` is present only when the worker turn carried one.
    """

    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    seq: int = Field(ge=0)
    kind: AgentLogKind
    text: str = Field(max_length=AGENT_LOG_TEXT_MAX_CHARS)


class AgentLogWriter:
    """Write one attempt's ``agent_log_line`` events with ``seq`` counted from zero.

    Every text is redacted with ``secrets`` and then bounded to ``AGENT_LOG_TEXT_MAX_CHARS``.
    """

    def __init__(self, *, attempt_id: str, task_id: str | None, secrets: Iterable[str]):
        self._attempt_id = attempt_id
        self._task_id = task_id
        self._secrets = tuple(secrets)
        self._seq = 0

    def write(self, kind: AgentLogKind, text: str) -> None:
        """Log the next line of the attempt."""
        # Redaction comes first: a bound cut through a secret would leave a prefix
        # the redactor no longer recognises.
        text = redact_diagnostic(text, secrets=self._secrets)
        if len(text) > AGENT_LOG_TEXT_MAX_CHARS:
            text = text[: AGENT_LOG_TEXT_MAX_CHARS - 1] + "…"
        line = AgentLogLine(
            attempt_id=self._attempt_id,
            task_id=self._task_id,
            seq=self._seq,
            kind=kind,
            text=text,
        )
        self._seq += 1
        logger.info(AGENT_LOG_LINE_EVENT, **line.model_dump(mode="json"))
