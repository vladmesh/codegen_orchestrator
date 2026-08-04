"""Temporary access grant schemas."""

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.run_result import QARunResult

# The request schemas are the contract every client already imports; the API
# validates against those same objects rather than look-alikes of its own.
from shared.contracts.dto.temporary_access import (
    TemporaryAccessGrantCreate,
    TemporaryAccessGrantUpdate,
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.queues.qa import QAMessage

__all__ = [
    "TemporaryAccessEscalation",
    "TemporaryAccessGrantCreate",
    "TemporaryAccessGrantRead",
    "TemporaryAccessGrantUpdate",
]


class TemporaryAccessEscalation(BaseModel):
    """Give up on a quiet revoke and make the QA run say so, in one write.

    The failure to take the access back belongs to the QA run that borrowed it:
    cleanup is part of that run, not a side effect after it. So the run's
    outcome and the grant's escalation stamp are one decision, taken in one
    transaction — a crash between them used to leave a story waiting on a grant
    that had already given up.
    """

    model_config = ConfigDict(extra="forbid")

    error: str = Field(min_length=1)
    run_error_message: str = Field(min_length=1)
    run_result: QARunResult


class TemporaryAccessGrantRead(TimestampedDTO):
    """A grant as stored."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: uuid.UUID
    env_key: str
    subject: str
    head_sha: str
    qa_run_id: str
    grant_run_id: str
    qa_message: QAMessage
    status: TemporaryAccessStatus
    granted_at: datetime
    qa_dispatched_at: datetime | None = None
    revoked_at: datetime | None = None
    revoke_reason: TemporaryAccessRevokeReason | None = None
    revoke_run_id: str | None = None
    revoke_attempts: int = 0
    escalated_at: datetime | None = None
    last_error: str | None = None
    observed_at: datetime | None = None
    observation_id: str | None = None
    slot_clear_since: datetime | None = None
    slot_clear_readings: int = 0
    reopened_at: datetime | None = None
