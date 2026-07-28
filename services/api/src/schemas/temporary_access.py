"""Temporary access grant schemas."""

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.run_result import QARunResult
from shared.contracts.dto.temporary_access import (
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.git_ref import CommitSha
from shared.contracts.queues.qa import QAMessage


class TemporaryAccessGrantCreate(BaseModel):
    """Register a grant before the deploy that hands the access out."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    project_id: uuid.UUID
    env_key: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    head_sha: CommitSha
    qa_run_id: str = Field(min_length=1)
    grant_run_id: str = Field(min_length=1)
    qa_message: QAMessage


class TemporaryAccessGrantUpdate(BaseModel):
    """Move a grant along as the reconciler settles it.

    `qa_dispatched` and `escalated` are requests to stamp a moment, not values
    to write: the record keeps when the handoff was released and when the sweep
    reported the access as still standing.
    """

    model_config = ConfigDict(extra="forbid")

    status: TemporaryAccessStatus | None = None
    grant_run_id: str | None = None
    qa_dispatched: bool | None = None
    revoke_reason: TemporaryAccessRevokeReason | None = None
    revoke_run_id: str | None = None
    revoke_attempts: int | None = None
    escalated: bool | None = None
    last_error: str | None = None


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
