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

from datetime import datetime, timedelta
from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.contracts.dto.base import TimestampedDTO
from shared.contracts.git_ref import CommitSha
from shared.contracts.queues.qa import QAMessage

# A reading of the running service is a moment, not a state. The deploy that
# clears the value is handed to GitHub Actions, which is asynchronous and not
# ours, so a writer carrying the old value can still land after an empty reading
# was taken. One empty reading therefore closes nothing; several taken apart over
# this span do. This is the span the guarantee is written in: the access does not
# outlive one reconciliation interval after it is seen.
#
# Closing the grant does not stop the readings either. The sweep keeps reading
# the slot for a while after the record says REVOKED, and a value found there
# puts the grant back under reconciliation with OBSERVED_AFTER_REVOKE.
REVOKE_CONFIRMATION_WINDOW = timedelta(minutes=10)
REVOKE_CONFIRMATION_READINGS = 2


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
    # The readings had agreed the slot was empty and the grant was closed, and a
    # later reading found the value again — a late dispatch that landed after the
    # confirmation, or a write from outside. Either way it is a value that should
    # not be there, and the grant goes back under reconciliation.
    OBSERVED_AFTER_REVOKE = "observed_after_revoke"


class TemporaryAccessGrantCreate(BaseModel):
    """Write the grant down before the deploy that hands the access out."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    project_id: uuid.UUID
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
    """Fields the reconciler moves as a grant settles.

    Every status except REVOKED. Asking for REVOKED here is asking for the record
    to say the access is gone because a caller believes it, and no caller can:
    what the deployed service holds is read from the server, and the reading is
    what closes the grant. So the only way in is
    ``POST /temporary-access-grants/{id}/observation``, and this refuses.
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

    @model_validator(mode="after")
    def _revoked_is_not_a_field_to_set(self) -> TemporaryAccessGrantUpdate:
        if self.status is TemporaryAccessStatus.REVOKED:
            raise ValueError(
                "a grant is revoked by an observation of the running service, not by an update; "
                "post the reading to /temporary-access-grants/{id}/observation"
            )
        return self


class TemporaryAccessObservation(BaseModel):
    """One reading of the environment a deployed service is actually running with.

    This is the only evidence that closes a grant. It names which application was
    read, because a project may run on several servers and a clear slot on the
    wrong one says nothing about the bot the QA run tested.

    ``containers`` is at least one by contract: a service with nothing running has
    no environment to read, and the reading channel reports that as unreachable
    rather than as an empty slot.
    """

    model_config = ConfigDict(extra="forbid")

    # Names this reading, so a caller repeating one it could not confirm is
    # counted once rather than twice towards the confirmation.
    observation_id: str = Field(min_length=1)
    application_id: int
    server_handle: str = Field(min_length=1)
    service_slug: str = Field(min_length=1)
    env_key: str = Field(min_length=1)
    present: bool
    containers: int = Field(ge=1)


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
    # The last reading of the running service, and which one it was. The moment
    # paces the next question; the id makes a repeated answer count once.
    observed_at: datetime | None = None
    observation_id: str | None = None
    # When the readings started agreeing the slot is empty, and how many have
    # agreed since. A reading that finds the value again puts both back to the
    # start, because the streak is what the confirmation is made of.
    slot_clear_since: datetime | None = None
    slot_clear_readings: int = 0
    # When a reading put a closed grant back under reconciliation. The retry
    # budget and the age at which an unfinished revoke goes to a human are
    # counted from here, because a returned value is a new disagreement rather
    # than a continuation of the one that was already settled.
    reopened_at: datetime | None = None

    @model_validator(mode="after")
    def _revoked_grant_carries_its_evidence(self) -> TemporaryAccessGrantDTO:
        if self.status is TemporaryAccessStatus.REVOKED:
            if self.revoked_at is None or self.revoke_reason is None:
                raise ValueError("a revoked grant must carry revoked_at and revoke_reason")
            if self.observation_id is None:
                raise ValueError("a revoked grant must name the reading that closed it")
        return self
