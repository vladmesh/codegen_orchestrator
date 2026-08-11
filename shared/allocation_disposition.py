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
