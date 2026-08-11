"""The one table of what each refused placement must do, on each path.

The invariant "an allocation refusal never terminates a user story" spans two
services, and so does the invariant that no path may answer two refusal
dispositions the same way. The deploy consumer in `langgraph` writes the run
result, and the scheduler routes it; their unit suites cannot import each other,
so the contract between them is pinned here instead of restated on both sides:

- `services/langgraph/tests/unit/consumers/test_deploy_infrastructure_wait.py`
  asserts that the consumer writes exactly the result described here;
- `services/scheduler/tests/unit/test_supervisor_run_routing.py` feeds that
  result to the real deploy routing, case by case;
- `services/scheduler/tests/unit/test_supervisor.py` drives the engineering
  routing with the same cases;
- `shared/tests/unit/test_allocation_disposition.py` asserts that the production
  table in `shared/allocation_disposition.py` agrees with this one.

**Each case carries its own expected behaviour.** The previous version of this
file described one expectation for every reason, which is why the suite could
not catch the deploy path collapsing four dispositions into a single endless
wait — it asserted the collapse as the contract. Expectations here are written
out by hand rather than derived from the production table, so editing that table
alone fails the suite, and two dispositions that start behaving identically fail
`test_expected_behaviours_are_distinct_per_path`.
"""

from dataclasses import dataclass

from shared.allocation_disposition import AttemptDisposition, RefusalRouting
from shared.contracts.dto.run_result import AllocationFailureReason, DeployRunResult
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployOutcome

#: The admission budget a refused deploy attempt asked for.
REFUSED_DEPLOY_REQUIRED_RAM_MB = 768
REFUSED_DEPLOY_MIN_DISK_MB = 1024

#: The story action that reaches the human-review queue. It is an API action
#: endpoint, not a status value; posting the status reaches nothing.
HUMAN_REVIEW_ACTION = "human-review"


@dataclass(frozen=True)
class EngineeringExpectation:
    """What the engineering path owes one disposition."""

    routing: RefusalRouting
    #: Where the failed task ends up, or None when the caller's retry keeps it.
    task_status: TaskStatus | None
    #: The story transition, or None when the story is left where it is.
    story_action: str | None
    admin_alerted: bool
    #: The PO event the owner receives, or None when the owner is not told.
    owner_event: str | None
    #: Whether `_park_task_waiting_resources` claims the task at all.
    handled: bool


@dataclass(frozen=True)
class DeployExpectation:
    """What the deploy path owes one disposition."""

    routing: RefusalRouting
    story_action: str | None
    admin_alerted: bool
    owner_event: str | None
    #: The counter `supervise_deploying_stories` advances while no server in the
    #: fleet is admissible.
    counter: str
    #: Whether the deploy is re-dispatched once a target becomes admissible.
    resumes_when_target_admissible: bool


@dataclass(frozen=True)
class RefusalRoutingCase:
    reason: AllocationFailureReason
    disposition: AttemptDisposition
    engineering: EngineeringExpectation
    deploy: DeployExpectation


_PARK = EngineeringExpectation(
    routing=RefusalRouting.PARK_WAITING_RESOURCES,
    task_status=TaskStatus.WAITING_RESOURCES,
    story_action=None,
    admin_alerted=False,
    owner_event="task_waiting_resources",
    handled=True,
)
_PARK_UNPROVISIONED = EngineeringExpectation(
    routing=RefusalRouting.PARK_WAITING_RESOURCES,
    task_status=TaskStatus.WAITING_RESOURCES,
    story_action=None,
    admin_alerted=False,
    # The same wait, but the owner must not be told the platform ran out of
    # capacity when what actually happened is an unfinished host build.
    owner_event="task_waiting_infrastructure",
    handled=True,
)
_ENGINEERING_OPERATOR_REVIEW = EngineeringExpectation(
    routing=RefusalRouting.HUMAN_REVIEW_WITH_OWNER_NOTICE,
    task_status=TaskStatus.WAITING_HUMAN_REVIEW,
    story_action=HUMAN_REVIEW_ACTION,
    admin_alerted=True,
    owner_event="task_impossible_capacity",
    handled=True,
)
_ENGINEERING_PLATFORM_ALERT = EngineeringExpectation(
    routing=RefusalRouting.HUMAN_REVIEW_PLATFORM_ALERT,
    task_status=TaskStatus.WAITING_HUMAN_REVIEW,
    story_action=HUMAN_REVIEW_ACTION,
    admin_alerted=True,
    # Nothing for the owner to decide: the platform cannot see its own fleet.
    owner_event=None,
    handled=True,
)

_DEPLOY_WAIT = DeployExpectation(
    routing=RefusalRouting.WAIT_FOR_ADMISSIBLE_TARGET,
    story_action=None,
    admin_alerted=False,
    owner_event=None,
    counter="waiting",
    resumes_when_target_admissible=True,
)
_DEPLOY_OPERATOR_REVIEW = DeployExpectation(
    routing=RefusalRouting.HUMAN_REVIEW_WITH_OWNER_NOTICE,
    story_action=HUMAN_REVIEW_ACTION,
    admin_alerted=True,
    owner_event="story_impossible_capacity",
    counter="escalated",
    # A request no server can ever fit is not waiting for one to appear.
    resumes_when_target_admissible=False,
)
_DEPLOY_PLATFORM_ALERT = DeployExpectation(
    routing=RefusalRouting.HUMAN_REVIEW_PLATFORM_ALERT,
    story_action=HUMAN_REVIEW_ACTION,
    admin_alerted=True,
    owner_event=None,
    counter="escalated",
    resumes_when_target_admissible=False,
)


#: Reason → disposition → behaviour on each path. Every `AllocationFailureReason`
#: appears exactly once; `test_every_allocation_reason_has_a_case` fails if a new
#: one is added without deciding what it does.
REFUSAL_ROUTING_CASES: tuple[RefusalRoutingCase, ...] = (
    RefusalRoutingCase(
        reason=AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
        disposition=AttemptDisposition.INFRASTRUCTURE_WAIT,
        engineering=_PARK,
        deploy=_DEPLOY_WAIT,
    ),
    RefusalRoutingCase(
        reason=AllocationFailureReason.INSUFFICIENT_RESERVED_MEMORY,
        disposition=AttemptDisposition.INFRASTRUCTURE_WAIT,
        engineering=_PARK,
        deploy=_DEPLOY_WAIT,
    ),
    RefusalRoutingCase(
        reason=AllocationFailureReason.SERVER_NOT_PROVISIONED,
        disposition=AttemptDisposition.INFRASTRUCTURE_WAIT,
        engineering=_PARK_UNPROVISIONED,
        deploy=_DEPLOY_WAIT,
    ),
    RefusalRoutingCase(
        reason=AllocationFailureReason.IMPOSSIBLE_CAPACITY,
        disposition=AttemptDisposition.OPERATOR_REVIEW,
        engineering=_ENGINEERING_OPERATOR_REVIEW,
        deploy=_DEPLOY_OPERATOR_REVIEW,
    ),
    RefusalRoutingCase(
        reason=AllocationFailureReason.NO_FRESH_METRICS,
        disposition=AttemptDisposition.TECHNICAL_FAILURE,
        engineering=_ENGINEERING_PLATFORM_ALERT,
        deploy=_DEPLOY_PLATFORM_ALERT,
    ),
)

#: Every allocation reason a deploy can be refused for, in case order.
REFUSED_DEPLOY_REASONS: tuple[AllocationFailureReason, ...] = tuple(
    case.reason for case in REFUSAL_ROUTING_CASES
)


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
