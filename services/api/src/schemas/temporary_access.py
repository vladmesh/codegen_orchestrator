"""Temporary access grant schemas."""

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.dto.temporary_access import (
    TemporaryAccessRevokeReason,
    TemporaryAccessStatus,
)
from shared.contracts.git_ref import CommitSha


class TemporaryAccessGrantCreate(BaseModel):
    """Register a grant before the deploy that hands the access out."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    project_id: uuid.UUID
    env_key: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    head_sha: CommitSha
    qa_run_id: str = Field(min_length=1)


class TemporaryAccessGrantUpdate(BaseModel):
    """Move a grant along as the reconciler settles it."""

    model_config = ConfigDict(extra="forbid")

    status: TemporaryAccessStatus | None = None
    revoke_reason: TemporaryAccessRevokeReason | None = None
    revoke_run_id: str | None = None
    revoke_attempts: int | None = None
    last_error: str | None = None


class TemporaryAccessGrantRead(TimestampedDTO):
    """A grant as stored."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: uuid.UUID
    env_key: str
    subject: str
    head_sha: str
    qa_run_id: str
    status: TemporaryAccessStatus
    granted_at: datetime
    revoked_at: datetime | None = None
    revoke_reason: TemporaryAccessRevokeReason | None = None
    revoke_run_id: str | None = None
    revoke_attempts: int = 0
    last_error: str | None = None
