"""What a failed placement attempt is allowed to do to a user's story.

**Invariant: an allocation refusal never terminates a user story.** It leads to a
wait or to another infrastructure outcome, and to nothing else. Every member of
:class:`~shared.contracts.dto.run_result.AllocationFailureReason` is a statement
about the platform's servers — none of them is evidence that the user's project
is broken — so none of them may end that project's story or raise a
product-failure alert.

This module is the one place that decision is made. Both routing paths ask it and
neither keeps a reason list of its own:

- engineering — ``services/scheduler/src/tasks/supervisor.py::_park_task_waiting_resources``,
  which decides whether a failed task parks instead of retrying the code;
- deploy — ``services/langgraph/src/consumers/deploy.py::process_deploy_job``, which
  decides what outcome the deploy run records, and
  ``services/scheduler/src/tasks/supervisor.py::supervise_deploying_stories``,
  which routes that outcome.

The two paths *react* differently, because they hold different things: the
engineering path has a task to park and an owner to tell, the deploy path has a
run to re-dispatch. What they may not do differs in nothing: an infrastructure
disposition never reaches `fail_story` or a product-failure notification.

**Second invariant: no path may answer two dispositions the same way.** The
dispositions exist because they need different handling — waiting out a capacity
shortage is not the same act as telling an operator that a request will never
fit — so a path that collapses them has silently deleted one of them. That was a
real defect: the deploy path recorded every refusal as one infrastructure wait,
which left a request no server could ever satisfy polling forever with nobody
told. :data:`REFUSAL_ROUTING` therefore states one behaviour per disposition per
path, and `shared/tests/unit/test_allocation_disposition.py` fails if any path's
row stops being injective.
"""

from enum import StrEnum

from shared.contracts.dto.run_result import AllocationFailureReason


class AttemptDisposition(StrEnum):
    """What a failed placement attempt is, once classified."""

    # The platform can serve this request later without anybody deciding anything:
    # memory frees up, a host finishes provisioning. Park and resume.
    INFRASTRUCTURE_WAIT = "infrastructure_wait"
    # No managed server could ever fit this request as it stands. Still not the
    # project's defect — it needs an operator, not a code change.
    OPERATOR_REVIEW = "operator_review"
    # The platform cannot see its own servers well enough to admit anything. An
    # observability failure, kept on the technical path rather than the wait.
    TECHNICAL_FAILURE = "technical_failure"
    # Nothing infrastructural happened; the caller's own routing applies.
    PRODUCT_FAILURE = "product_failure"
    # Neither an allocation refusal nor a product failure.
    NONE = "none"


#: Every allocation reason is classified explicitly. A new member of
#: `AllocationFailureReason` that nobody classified raises `KeyError` at the first
#: routing decision instead of silently defaulting into the product path.
ALLOCATION_DISPOSITIONS: dict[AllocationFailureReason, AttemptDisposition] = {
    AllocationFailureReason.INSUFFICIENT_FREE_MEMORY: AttemptDisposition.INFRASTRUCTURE_WAIT,
    AllocationFailureReason.INSUFFICIENT_RESERVED_MEMORY: AttemptDisposition.INFRASTRUCTURE_WAIT,
    AllocationFailureReason.SERVER_NOT_PROVISIONED: AttemptDisposition.INFRASTRUCTURE_WAIT,
    AllocationFailureReason.IMPOSSIBLE_CAPACITY: AttemptDisposition.OPERATOR_REVIEW,
    AllocationFailureReason.NO_FRESH_METRICS: AttemptDisposition.TECHNICAL_FAILURE,
}

#: The dispositions that describe the platform rather than the project. None of
#: them may terminate a story.
INFRASTRUCTURE_DISPOSITIONS: frozenset[AttemptDisposition] = frozenset(
    {
        AttemptDisposition.INFRASTRUCTURE_WAIT,
        AttemptDisposition.OPERATOR_REVIEW,
        AttemptDisposition.TECHNICAL_FAILURE,
    }
)


def attempt_disposition(
    allocation_failure: AllocationFailureReason | None,
    *,
    product_failure: bool,
) -> AttemptDisposition:
    """Classify one failed attempt, allocation refusal first.

    The precedence is stated here rather than left to the order of branches in
    two callers: when the same attempt carries both an allocation refusal and a
    product failure, the allocation refusal wins and the story is not terminated.
    A server the platform could not provide is never evidence that the user's
    project is broken, and the reverse mistake is the expensive one — it sends a
    correct project back to an engineering worker, or tells its owner it failed.
    """
    if allocation_failure is not None:
        return ALLOCATION_DISPOSITIONS[allocation_failure]
    if product_failure:
        return AttemptDisposition.PRODUCT_FAILURE
    return AttemptDisposition.NONE


def may_terminate_story(disposition: AttemptDisposition) -> bool:
    """Whether this disposition is allowed to end the user's story.

    Only a product failure is. This is the invariant itself, in one expression,
    for a caller that wants to state it rather than re-derive it.
    """
    return disposition is AttemptDisposition.PRODUCT_FAILURE


class PlacementPath(StrEnum):
    """The two places a placement attempt can be routed from."""

    ENGINEERING = "engineering"
    DEPLOY = "deploy"


class RefusalRouting(StrEnum):
    """What a path does with one disposition — exactly one behaviour each.

    The members are behaviours, not restatements of the dispositions: two
    dispositions sharing a member on one path *is* the collapse this table
    exists to prevent, and the suite says so.
    """

    # Park the engineering task in `waiting_resources`. The wait ends by itself
    # when a target becomes admissible again, and is bounded by
    # `supervisor.resource_wait_timeout_minutes`, after which a human is told.
    PARK_WAITING_RESOURCES = "park_waiting_resources"
    # Keep the story DEPLOYING and re-dispatch the deploy once a target is
    # admissible. Bounded by the same timeout, for the same reason.
    WAIT_FOR_ADMISSIBLE_TARGET = "wait_for_admissible_target"
    # No wait can resolve this: the request does not fit any managed server as
    # it stands. Move the work to the human-review queue at once, alert
    # operators, and tell the owner — they may want to change what they asked
    # for, and they are owed the reason it stopped.
    HUMAN_REVIEW_WITH_OWNER_NOTICE = "human_review_with_owner_notice"
    # The platform cannot evaluate its own fleet, so it can neither wait (the
    # re-check needs the very metrics that are missing) nor usefully retry.
    # Operators are alerted and the work waits for a human; the owner is not
    # asked to do anything, because there is nothing for them to decide.
    HUMAN_REVIEW_PLATFORM_ALERT = "human_review_platform_alert"
    # Not an allocation refusal: the caller's own failure routing applies, which
    # for a product failure is the only path allowed to end a story.
    CALLER_FAILURE_ROUTING = "caller_failure_routing"
    # Nothing failed in a way this table describes.
    NO_REFUSAL = "no_refusal"


#: Disposition × path → behaviour. Total in both directions: every path names
#: every disposition, so a routing decision cannot fall through to a default,
#: and no path repeats a behaviour, so no two dispositions can be answered the
#: same way. The two paths differ only where they must — an engineering task is
#: parked, a deploy run is re-dispatched — and agree everywhere else.
REFUSAL_ROUTING: dict[PlacementPath, dict[AttemptDisposition, RefusalRouting]] = {
    PlacementPath.ENGINEERING: {
        AttemptDisposition.INFRASTRUCTURE_WAIT: RefusalRouting.PARK_WAITING_RESOURCES,
        AttemptDisposition.OPERATOR_REVIEW: RefusalRouting.HUMAN_REVIEW_WITH_OWNER_NOTICE,
        AttemptDisposition.TECHNICAL_FAILURE: RefusalRouting.HUMAN_REVIEW_PLATFORM_ALERT,
        AttemptDisposition.PRODUCT_FAILURE: RefusalRouting.CALLER_FAILURE_ROUTING,
        AttemptDisposition.NONE: RefusalRouting.NO_REFUSAL,
    },
    PlacementPath.DEPLOY: {
        AttemptDisposition.INFRASTRUCTURE_WAIT: RefusalRouting.WAIT_FOR_ADMISSIBLE_TARGET,
        AttemptDisposition.OPERATOR_REVIEW: RefusalRouting.HUMAN_REVIEW_WITH_OWNER_NOTICE,
        AttemptDisposition.TECHNICAL_FAILURE: RefusalRouting.HUMAN_REVIEW_PLATFORM_ALERT,
        AttemptDisposition.PRODUCT_FAILURE: RefusalRouting.CALLER_FAILURE_ROUTING,
        AttemptDisposition.NONE: RefusalRouting.NO_REFUSAL,
    },
}


def refusal_routing(path: PlacementPath, disposition: AttemptDisposition) -> RefusalRouting:
    """The one behaviour this path owes this disposition.

    Callers branch on the answer instead of on the disposition, so a new
    disposition — or a path that forgot one — raises `KeyError` here rather than
    quietly reusing whatever branch happened to be last.
    """
    return REFUSAL_ROUTING[path][disposition]
