"""Durable record of a temporary environment access grant.

Handing a test identity access to a deployed bot is an external effect that
outlives the process that produced it. The record is written before the effect,
so revocation can be decided by reading state (a live grant whose run is over
must be revoked) instead of by reaching the end of a happy path. A process that
dies between grant and revoke leaves the record, and the record is what drives
the revoke.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.git_ref import CommitSha


class TemporaryAccessStatus(StrEnum):
    """Lifecycle of one grant, as stored."""

    GRANTED = "granted"
    REVOKING = "revoking"
    REVOKED = "revoked"
    REVOKE_FAILED = "revoke_failed"


# Everything that is not REVOKED still holds access on the deployed application
# and must keep appearing in the reconciliation sweep.
LIVE_TEMPORARY_ACCESS_STATUSES: frozenset[TemporaryAccessStatus] = frozenset(
    {
        TemporaryAccessStatus.GRANTED,
        TemporaryAccessStatus.REVOKING,
        TemporaryAccessStatus.REVOKE_FAILED,
    }
)


class TemporaryAccessRevokeReason(StrEnum):
    """Why the sweep decided this grant must go."""

    RUN_TERMINAL = "run_terminal"
    RUN_MISSING = "run_missing"
    EXPIRED = "expired"


class TemporaryAccessGrantCreate(BaseModel):
    """Write the grant down before the deploy that hands the access out."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    env_key: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    # The commit the access was deployed on. Revoking redeploys that same commit
    # with the value cleared, so the bot that loses the access is the bot that
    # was given it.
    head_sha: CommitSha
    # The QA run the access exists for. Its terminal state is what makes the
    # grant revocable, so a grant without one could never be settled.
    qa_run_id: str = Field(min_length=1)


class TemporaryAccessGrantUpdate(BaseModel):
    """Fields the reconciler moves as a grant settles."""

    model_config = ConfigDict(extra="forbid")

    status: TemporaryAccessStatus | None = None
    revoke_reason: TemporaryAccessRevokeReason | None = None
    revoke_run_id: str | None = None
    revoke_attempts: int | None = None
    last_error: str | None = None


class TemporaryAccessGrantDTO(TimestampedDTO):
    """A grant as the reconciler reads it."""

    id: str
    project_id: str
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

    @model_validator(mode="after")
    def _revoked_grant_carries_its_evidence(self) -> TemporaryAccessGrantDTO:
        if self.status is TemporaryAccessStatus.REVOKED and (
            self.revoked_at is None or self.revoke_reason is None
        ):
            raise ValueError("a revoked grant must carry revoked_at and revoke_reason")
        return self
