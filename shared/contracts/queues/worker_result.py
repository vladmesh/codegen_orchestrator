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
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "WorkerResultStatus",
    "WorkerCompletedResult",
    "WorkerFailedResult",
    "WorkerBlockedResult",
    "WorkerResult",
    "WorkerResultAdapter",
    "parse_worker_result",
]


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


class WorkerCompletedResult(_WorkerResultBase):
    """Worker finished the task and committed code."""

    status: Literal[WorkerResultStatus.COMPLETED] = WorkerResultStatus.COMPLETED
    commit_sha: str
    content: str  # human-readable summary of the change


class WorkerFailedResult(_WorkerResultBase):
    """Worker hit a technical failure (execution error, timeout, no result)."""

    status: Literal[WorkerResultStatus.FAILED] = WorkerResultStatus.FAILED
    error: str


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
