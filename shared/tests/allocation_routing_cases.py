"""The one description of what a refused placement looks like on the wire.

The invariant "an allocation refusal never terminates a user story" spans two
services: the deploy consumer in `langgraph` writes the run result, and the
scheduler routes it. Their unit suites cannot import each other, so the contract
between them is pinned here instead of restated on both sides:

- `services/langgraph/tests/unit/consumers/test_deploy_infrastructure_wait.py`
  asserts that the consumer writes exactly this result;
- `services/scheduler/tests/unit/test_supervisor.py` feeds exactly this result to
  the real routing and asserts the story survives it.

If the producer stops emitting this shape, the first suite fails; if the router
stops handling it safely, the second does. Neither can drift alone.
"""

from shared.contracts.dto.run_result import AllocationFailureReason, DeployRunResult
from shared.contracts.queues.deploy import DeployOutcome

#: The admission budget a refused deploy attempt asked for.
REFUSED_DEPLOY_REQUIRED_RAM_MB = 768
REFUSED_DEPLOY_MIN_DISK_MB = 1024

#: Every allocation reason a deploy can be refused for. All of them are statements
#: about the platform's servers, so all of them must route the same way.
REFUSED_DEPLOY_REASONS: tuple[AllocationFailureReason, ...] = tuple(AllocationFailureReason)


def refused_deploy_result(
    reason: AllocationFailureReason = AllocationFailureReason.SERVER_NOT_PROVISIONED,
) -> DeployRunResult:
    """The run result a deploy that could not be placed has to record."""
    return DeployRunResult(
        deploy_outcome=DeployOutcome.WAITING_INFRASTRUCTURE,
        allocation_failure_reason=reason,
        allocation_required_ram_mb=REFUSED_DEPLOY_REQUIRED_RAM_MB,
        allocation_min_disk_mb=REFUSED_DEPLOY_MIN_DISK_MB,
    )
