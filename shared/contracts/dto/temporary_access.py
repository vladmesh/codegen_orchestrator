"""Durable record of a temporary environment access grant.

Handing a test identity access to a deployed bot is an external effect that
outlives the process that produced it. The record is written before the effect,
so revocation can be decided by reading state (a live grant whose run is over
must be revoked) instead of by reaching the end of a happy path. A process that
dies between grant and revoke leaves the record, and the record is what drives
the revoke.

The record also holds the QA handoff the grant was made for. QA is released by
the sweep once the deploy that applies the access has confirmed success, so a
lagging grant deploy can never land after the access was already taken back.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.git_ref import CommitSha
from shared.contracts.queues.qa import QAMessage


class TemporaryAccessStatus(StrEnum):
    """Lifecycle of one grant, as stored."""

    # The deploy that applies the value has been dispatched and has not confirmed.
    GRANTING = "granting"
    # That deploy reported success, so the access is on the deployed application.
    GRANTED = "granted"
    REVOKING = "revoking"
    REVOKED = "revoked"
    REVOKE_FAILED = "revoke_failed"


# Everything that is not REVOKED may hold access on the deployed application and
# must keep appearing in the reconciliation sweep. GRANTING counts: its deploy
# may already have applied the value even when it never reported back.
LIVE_TEMPORARY_ACCESS_STATUSES: frozenset[TemporaryAccessStatus] = frozenset(
    {
        TemporaryAccessStatus.GRANTING,
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
    # The deploy meant to hand the access out never confirmed, so whether the
    # value landed is unknown and the slot must be cleared either way.
    GRANT_FAILED = "grant_failed"


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
    # The deploy run that applies the value. Until it confirms, nothing may
    # assume the access is in place and nothing may assume it is not.
    grant_run_id: str = Field(min_length=1)
    # The handoff this grant is holding. QA starts from the record, not from the
    # process that asked for the access, so a restart cannot lose the run.
    qa_message: QAMessage


class TemporaryAccessGrantUpdate(BaseModel):
    """Fields the reconciler moves as a grant settles."""

    model_config = ConfigDict(extra="forbid")

    status: TemporaryAccessStatus | None = None
    grant_run_id: str | None = None
    qa_dispatched: bool | None = None
    revoke_reason: TemporaryAccessRevokeReason | None = None
    revoke_run_id: str | None = None
    revoke_attempts: int | None = None
    escalated: bool | None = None
    last_error: str | None = None


class TemporaryAccessGrantDTO(TimestampedDTO):
    """A grant as the reconciler reads it."""

    id: str
    project_id: str
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
    # When the sweep stopped expecting a quiet revoke and reported the access as
    # still standing. Retries continue; what changes is that the QA run and its
    # story no longer wait for them.
    escalated_at: datetime | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def _revoked_grant_carries_its_evidence(self) -> TemporaryAccessGrantDTO:
        if self.status is TemporaryAccessStatus.REVOKED and (
            self.revoked_at is None or self.revoke_reason is None
        ):
            raise ValueError("a revoked grant must carry revoked_at and revoke_reason")
        return self
