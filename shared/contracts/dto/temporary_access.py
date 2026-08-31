"""Durable, revocable QA admission for one generated service target."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.git_ref import CommitSha
from shared.contracts.queues.qa import QAMessage


class TemporaryAccessStatus(StrEnum):
    """Lifecycle recorded before each remote capability effect."""

    GRANTING = "granting"
    GRANTED = "granted"
    REVOKING = "revoking"
    REVOKED = "revoked"
    REVOKE_FAILED = "revoke_failed"


LIVE_TEMPORARY_ACCESS_STATUSES: frozenset[TemporaryAccessStatus] = frozenset(
    {
        TemporaryAccessStatus.GRANTING,
        TemporaryAccessStatus.GRANTED,
        TemporaryAccessStatus.REVOKING,
        TemporaryAccessStatus.REVOKE_FAILED,
    }
)


class TemporaryAccessRevokeReason(StrEnum):
    RUN_TERMINAL = "run_terminal"
    RUN_MISSING = "run_missing"
    EXPIRED = "expired"
    GRANT_FAILED = "grant_failed"


class TemporaryAccessGrantCreate(BaseModel):
    """The complete immutable identity and target needed to settle QA access."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    project_id: uuid.UUID
    channel: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    target_application_id: int = Field(ge=1)
    target_base_url: str = Field(min_length=1)
    head_sha: CommitSha
    qa_run_id: str = Field(min_length=1)
    grant_run_id: str = Field(min_length=1)
    qa_message: QAMessage


class TemporaryAccessGrantUpdate(BaseModel):
    """State owned by the reconciler after a proved capability operation."""

    model_config = ConfigDict(extra="forbid")

    status: TemporaryAccessStatus | None = None
    grant_run_id: str | None = None
    grant_attempts: int | None = Field(default=None, ge=1)
    qa_dispatched: bool | None = None
    revoke_reason: TemporaryAccessRevokeReason | None = None
    revoke_run_id: str | None = None
    revoke_attempts: int | None = None
    escalated: bool | None = None
    last_error: str | None = None


class TemporaryAccessGrantDTO(TimestampedDTO):
    """A non-secret durable QA grant record."""

    id: str
    project_id: str
    channel: str
    external_id: str
    target_application_id: int
    target_base_url: str
    head_sha: str
    qa_run_id: str
    grant_run_id: str
    grant_attempts: int = 1
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
