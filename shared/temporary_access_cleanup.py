"""Whether a temporary-access cleanup must first stop an earlier grant's deploy workflow."""

from __future__ import annotations

from shared.contracts.dto.deploy_dispatch import DISPATCH_CLAIMED_AT_KEY
from shared.contracts.dto.run import RunDTO, RunStatus
from shared.contracts.queues.deploy import DeployOutcome


def grant_workflow_may_still_act(grant_run: RunDTO | None) -> bool:
    """Whether the grant operation Run may have left a deploy workflow that can still write.

    A Run that crossed the dispatch boundary counts as outside the system unless it
    completed SUCCESS, which waited for its workflow. A missing Run proves nothing.
    """
    if grant_run is None:
        return True
    if DISPATCH_CLAIMED_AT_KEY not in grant_run.run_metadata:
        return False
    return not (
        grant_run.status is RunStatus.COMPLETED
        and grant_run.result is not None
        and grant_run.result.deploy_outcome is DeployOutcome.SUCCESS
    )
