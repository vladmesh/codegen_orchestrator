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

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from shared.contracts.dto.run import RunStatus

# run_metadata key holding the moment the deploy worker crossed the boundary.
DISPATCH_CLAIMED_AT_KEY = "dispatch_claimed_at"


class DeployDispatchClaim(BaseModel):
    """Answer to a worker asking whether it may still dispatch."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    granted: bool
    run_status: RunStatus
    claimed_at: datetime | None = None


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
