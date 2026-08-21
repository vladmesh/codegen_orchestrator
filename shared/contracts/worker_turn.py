"""Durable identity for the one input turn a worker currently leases."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AttemptTurnMetadata", "WorkerActiveTurn", "active_turn_key"]


def active_turn_key(worker_id: str) -> str:
    """Redis hash holding the turn currently leased by this worker."""
    return f"worker:active-turn:{worker_id}"


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

    def as_run_metadata(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_run_metadata(cls, metadata: dict[str, Any] | None) -> "AttemptTurnMetadata":
        return cls.model_validate(metadata or {})
