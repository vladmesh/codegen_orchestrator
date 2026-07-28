"""The boundary a deploy run crosses when it starts work outside the system.

Cancelling a run is only a stop while the run has not yet reached GitHub. Once
the deploy worker has dispatched, the effect lives on GitHub Actions and has to
be stopped there instead. The gap between the two is where a revoke used to lose
its fence: the worker had decided to dispatch, the Actions run did not exist yet,
and the listing the fence reads therefore showed nothing to stop.

So the crossing is recorded on the run itself, and the record is written under a
row lock. A worker claims the boundary before it dispatches and a withdrawal
takes it before the worker gets there; exactly one of the two wins, and the
loser is told which side it is on. A refused claim never dispatches. A
withdrawal that arrives late is told the deploy is already outside, so the
caller stops it where it now lives rather than assuming it never started.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.run import RunStatus

# run_metadata key holding the moment the deploy worker crossed the boundary.
DISPATCH_CLAIMED_AT_KEY = "dispatch_claimed_at"

# run_metadata key holding the deadline the claim is good until.
DISPATCH_LEASE_EXPIRES_AT_KEY = "dispatch_lease_expires_at"

# run_metadata key holding the moment reconciliation took a dead claim back.
DISPATCH_SUPERSEDED_AT_KEY = "dispatch_superseded_at"

# How long a claim entitles its holder to dispatch. A claim is taken directly
# before the call that reaches GitHub, so this covers a couple of HTTP calls
# with room to spare; the holder re-reads the clock immediately before
# dispatching and refuses once the deadline has passed. That refusal is what
# makes the deadline mean something to a reader: past it, a worker that has
# gone quiet can no longer produce the effect, so its claim may be taken back
# instead of waited on forever.
DISPATCH_LEASE = timedelta(minutes=5)


class DeployDispatchClaim(BaseModel):
    """Answer to a worker asking whether it may still dispatch."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    granted: bool
    run_status: RunStatus
    claimed_at: datetime | None = None
    # Until when this claim entitles its holder to dispatch. Renewed on every
    # claim, so a worker that asks again gets a fresh deadline while the first
    # crossing recorded in `claimed_at` stays where it was.
    lease_expires_at: datetime | None = None


class DeployRunStart(BaseModel):
    """Answer to a worker asking whether it may begin work on a run at all.

    Taking a run to RUNNING is the first thing a worker does with it, and doing
    it with a blind write is how a cancelled run comes back to life: the worker
    read the run, a withdrawal cancelled it, and the write put it back into a
    state the dispatch claim accepts. So the transition is decided against the
    locked row like the claim is, and a run that is already terminal stays that
    way.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    started: bool
    run_status: RunStatus


class DispatchWithdrawal(StrEnum):
    """What a withdrawal found when it took the boundary."""

    # The run had not been claimed, so it is now cancelled and will never
    # dispatch. Nothing of it exists outside the system.
    WITHDRAWN = "withdrawn"
    # A worker had already claimed the boundary. The run is marked cancelled so
    # the worker stops its own Actions run, but the caller must treat the deploy
    # as live outside until the run reaches a terminal state.
    ALREADY_DISPATCHED = "already_dispatched"
    # The run had already finished, been cancelled, or failed. There is nothing
    # left to withdraw.
    ALREADY_TERMINAL = "already_terminal"


class DeployDispatchWithdrawal(BaseModel):
    """Answer to a caller trying to stop a deploy run before it reaches GitHub."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    outcome: DispatchWithdrawal
    run_status: RunStatus
    claimed_at: datetime | None = None


class DispatchSupersede(StrEnum):
    """What reconciliation found when it tried to take a claim back."""

    # The claim's lease had run out and the claimer never said what it did. The
    # boundary is now closed against it: it can neither dispatch nor re-claim.
    SUPERSEDED = "superseded"
    # The claimer recorded its own outcome first, which is the better answer.
    ALREADY_SETTLED = "already_settled"
    # Nobody ever crossed, so there is nothing outside to account for.
    NOT_CLAIMED = "not_claimed"
    # The lease is still good. The claimer may be about to dispatch and must be
    # given until its deadline before anything acts as though it will not.
    LEASE_LIVE = "lease_live"


class DeployDispatchSupersede(BaseModel):
    """Answer to reconciliation asking to take a silent claim back.

    A worker that dies between claiming the boundary and reporting what it did
    leaves a run nobody can settle, and everything waiting on that run waits for
    good. Waiting is only correct while the worker could still dispatch, and the
    lease is exactly how long that is. Past it the claim is taken back under the
    same row lock the claim was granted under, so the two can never both hold.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    outcome: DispatchSupersede
    run_status: RunStatus
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None

    @property
    def settled(self) -> bool:
        """Whether the dispatch can no longer produce an unseen external effect."""
        return self.outcome is not DispatchSupersede.LEASE_LIVE
