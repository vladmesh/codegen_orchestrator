"""Durable identity for the one input turn a worker currently leases."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["WorkerActiveTurn", "active_turn_key"]


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
