"""Typed developer-worker result contract.

Single strict schema for the result a developer worker publishes on its
``worker:{id}:output`` stream. The producer (worker-wrapper) builds one of these
models and publishes it; the consumer (langgraph ``worker_spawner``) validates
the raw payload against :data:`WorkerResult` before any business processing, so
status and content are never guessed from a set of synonymous keys.

The wire is a discriminated union keyed on ``status``:

- ``completed`` — code was written and committed (``commit_sha`` + ``content``).
- ``failed``    — execution error, timeout, or the agent exited without
  reporting (``error``).
- ``blocked`` / ``rejected`` — the worker gave up (``block_reason``). Both status
  values share one shape; the worker only emits ``blocked``, ``rejected`` stays
  accepted because the consumer historically treated the two identically.

``worker_report`` and ``agent_stdout_tail`` are optional metadata the wrapper may
attach to any result. ``extra="forbid"`` keeps the boundary strict: an unexpected
key is a poison payload, not a field to ignore.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from shared.contracts.dto.engineering_attempt import ClaudeResultEvidence

__all__ = [
    "WorkerResultStatus",
    "WorkerStopReason",
    "WorkerCompletedResult",
    "WorkerFailedResult",
    "WorkerBlockedResult",
    "WorkerResult",
    "WorkerResultAdapter",
    "parse_worker_result",
]


class WorkerStopReason(StrEnum):
    """Why the runtime deliberately ended a turn without a normal result."""

    AGENT_LIMIT_EXCEEDED = "agent_limit_exceeded"
    TURN_DEADLINE_EXCEEDED = "turn_deadline_exceeded"
    AGENT_REFUSED = "agent_refused"


class WorkerResultStatus(StrEnum):
    """Terminal status a developer worker reports for a task."""

    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REJECTED = "rejected"


class _WorkerResultBase(BaseModel):
    """Metadata the wrapper may attach to any worker result."""

    model_config = ConfigDict(extra="forbid")

    worker_report: str | None = None  # REPORT.md contents, if the worker wrote one
    agent_stdout_tail: str | None = None  # last ~10KB of agent stdout/stderr
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    claude_evidence: ClaudeResultEvidence | None = None
    transcript_path: str | None = None
    transcript_truncated: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _prevent_mixed_claude_facts(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("claude_evidence") is not None:
            mixed = {
                field
                for field in ("input_tokens", "output_tokens", "total_tokens", "cost_usd")
                if value.get(field) is not None
            }
            if mixed:
                raise ValueError("claude_evidence cannot be combined with flat effort metrics")
        return value


class WorkerCompletedResult(_WorkerResultBase):
    """Worker finished the task and committed code."""

    status: Literal[WorkerResultStatus.COMPLETED] = WorkerResultStatus.COMPLETED
    commit_sha: str
    content: str  # human-readable summary of the change


class WorkerFailedResult(_WorkerResultBase):
    """Worker hit a technical failure (execution error, timeout, no result).

    ``error`` is prose for a human. ``stop_reason`` is the same fact for the
    pipeline: it is what the attempt records in ``run_metadata``, so a run says
    *why* it stopped instead of only that it did. It is optional because most
    technical failures are not a stop at all — a crashed CLI, an unreadable
    workspace — and inventing a stop reason for them would make the field
    useless for the case it exists for.
    """

    status: Literal[WorkerResultStatus.FAILED] = WorkerResultStatus.FAILED
    error: str
    stop_reason: WorkerStopReason | None = None
    #: The limit that was in force, as the wrapper actually enforced it.
    agent_limit_seconds: int | None = None


class WorkerBlockedResult(_WorkerResultBase):
    """Worker gave up on the task (blocker hit or task refused)."""

    status: Literal[WorkerResultStatus.BLOCKED, WorkerResultStatus.REJECTED] = (
        WorkerResultStatus.BLOCKED
    )
    block_reason: str


WorkerResult = Annotated[
    WorkerCompletedResult | WorkerFailedResult | WorkerBlockedResult,
    Field(discriminator="status"),
]

WorkerResultAdapter: TypeAdapter[WorkerResult] = TypeAdapter(WorkerResult)


def parse_worker_result(data: dict) -> WorkerResult:
    """Validate a raw worker-output payload into a typed result model."""
    return WorkerResultAdapter.validate_python(data)
