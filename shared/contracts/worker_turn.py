"""Durable identity for the one input turn a worker currently leases."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.engineering_execution import EngineeringExecutionEvidence

__all__ = [
    "AttemptTurnMetadata",
    "PreparedCheckoutBaseline",
    "WorkerActiveTurn",
    "WorkerTurnInput",
    "active_turn_key",
]


def active_turn_key(worker_id: str) -> str:
    """Redis hash holding the turn currently leased by this worker."""
    return f"worker:active-turn:{worker_id}"


class WorkerTurnInput(BaseModel):
    """Typed wire envelope for one worker input-stream turn.

    Engineering turns carry an attempt id and a deadline together so the broker
    can fence the active lease. QA executor turns intentionally omit both and
    remain unsupervised by the engineering-attempt watchdog.

    task_id, content and user_id remain accepted for rolling compatibility with
    older queued payloads; current producers use prompt. Unknown fields are
    rejected at the broker boundary.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    attempt_id: str | None = Field(default=None, min_length=1)
    turn_deadline_seconds: int | None = Field(default=None, gt=0)
    prompt: str | None = Field(default=None, min_length=1)
    content: str | None = Field(default=None, min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    story_md: str | None = None
    branch: str | None = Field(default=None, min_length=1)
    clear_session: bool | None = None
    user_id: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_turn_shape(self) -> "WorkerTurnInput":
        if (self.attempt_id is None) != (self.turn_deadline_seconds is None):
            raise ValueError("attempt_id and turn_deadline_seconds must be provided together")
        if (self.prompt is None) == (self.content is None):
            raise ValueError("exactly one of prompt or content must be provided")
        return self


class WorkerActiveTurn(BaseModel):
    """A lease fenced to the attempt and request that received it.

    The broker creates it when it hands an input stream entry to the wrapper and
    removes it only after accepting the matching typed output.  It is evidence
    of ownership, not a claim that a model made semantic progress.
    """

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    lease_id: str = Field(min_length=1)
    started_at: datetime
    deadline_at: datetime

    def as_redis_fields(self) -> dict[str, str]:
        return {
            "worker_id": self.worker_id,
            "attempt_id": self.attempt_id,
            "request_id": self.request_id,
            "lease_id": self.lease_id,
            "started_at": self.started_at.isoformat(),
            "deadline_at": self.deadline_at.isoformat(),
        }

    @classmethod
    def from_redis_fields(cls, fields: dict[str, str] | None) -> "WorkerActiveTurn | None":
        if not fields:
            return None
        return cls.model_validate(fields)


class EngineeringTurnPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1)
    turn: WorkerTurnInput


class PreparedCheckoutBaseline(BaseModel):
    """Native checkout evidence, fenced to the worker's creator attempt."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class AttemptTurnMetadata(BaseModel):
    """The run-metadata half of a worker turn's durable identity.

    Run metadata also carries unrelated pipeline facts, so this model reads only
    its own fields and serializes only non-null values for a merge patch.
    """

    model_config = ConfigDict(extra="ignore")

    initiating_run_id: str | None = Field(default=None, min_length=1)
    worker_id: str | None = Field(default=None, min_length=1)
    agent_limit_seconds: int | None = Field(default=None, gt=0)
    active_turn_request_id: str | None = Field(default=None, min_length=1)
    active_turn_backstop_seconds: int | None = Field(default=None, gt=0)
    active_turn_requested_at: datetime | None = None
    worker_stop_requested_at: datetime | None = None
    worker_stop_attempts: int | None = Field(default=None, ge=0)
    worker_stop_next_retry_at: datetime | None = None
    stop_reason: str | None = None
    worker_state: str | None = None
    execution: EngineeringExecutionEvidence | None = None
    # The story branch head as it stood before this attempt's turn was sent, or
    # the default branch head when the branch did not exist yet. The developer's
    # initial lookup is reconciled with native preparation before the creator's
    # first turn; published turns retain their original saved baseline.
    pre_attempt_head_sha: str | None = Field(default=None, min_length=1)
    prepared_checkout: PreparedCheckoutBaseline | None = None

    def as_run_metadata(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_run_metadata(cls, metadata: dict[str, Any] | None) -> "AttemptTurnMetadata":
        return cls.model_validate(metadata or {})
