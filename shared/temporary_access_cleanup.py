"""Whether a temporary-access cleanup must first stop an earlier grant's deploy workflow."""

from __future__ import annotations

from collections.abc import Iterable

from shared.clients.run_api import RunAPIClientMixin
from shared.contracts.dto.deploy_dispatch import DISPATCH_CLAIMED_AT_KEY
from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.temporary_access import TemporaryAccessGrantDTO
from shared.contracts.queues.deploy import DeployOutcome


def _may_still_act(grant_run: RunDTO) -> bool:
    """A Run that crossed the dispatch boundary is outside until it completed SUCCESS."""
    if DISPATCH_CLAIMED_AT_KEY not in grant_run.run_metadata:
        return False
    return not (
        grant_run.status is RunStatus.COMPLETED
        and grant_run.result is not None
        and grant_run.result.deploy_outcome is DeployOutcome.SUCCESS
    )


def _grant_work_may_still_act(
    grant: TemporaryAccessGrantDTO, deploy_runs: Iterable[RunDTO]
) -> bool:
    """Whether any grant operation of this record may have left a workflow that can still write.

    Every grant operation Run of the record counts, not only the one it names now:
    replacing the operation id stops nothing. A history without the current grant
    Run proves nothing, so it owes the stop.
    """
    grant_runs = [
        run
        for run in deploy_runs
        if run.run_metadata.get("temporary_access_grant_id") == grant.id
        and run.run_metadata.get("temporary_access_operation") == "grant"
    ]
    if grant.grant_run_id not in {run.id for run in grant_runs}:
        return True
    return any(_may_still_act(run) for run in grant_runs)


async def grant_cleanup_owes_workflow_stop(
    api_client: RunAPIClientMixin, grant: TemporaryAccessGrantDTO
) -> bool:
    """Read the project's complete deploy Run history and judge every grant operation in it.

    An unreadable history raises; it is never an empty one.
    """
    return _grant_work_may_still_act(
        grant, await api_client.list_project_runs(grant.project_id, RunType.DEPLOY)
    )
