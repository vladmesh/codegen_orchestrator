"""One line of a coding agent's live log, as the worker wrapper writes it to its stdout."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

#: The structlog event name every live log line is written under.
AGENT_LOG_LINE_EVENT = "agent_log_line"

#: Upper bound of ``AgentLogLine.text``, applied after redaction.
AGENT_LOG_TEXT_MAX_CHARS = 2000


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
