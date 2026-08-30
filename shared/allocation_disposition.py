"""Classify allocation refusals without treating them as product failures."""

from enum import StrEnum

from shared.contracts.dto.run_result import AllocationFailureReason


class AttemptDisposition(StrEnum):
    """What a failed placement attempt is, once classified."""

    INFRASTRUCTURE_WAIT = "infrastructure_wait"
    OPERATOR_REVIEW = "operator_review"
    TECHNICAL_FAILURE = "technical_failure"
    PRODUCT_FAILURE = "product_failure"
    NONE = "none"


#: Every allocation refusal maps explicitly; unknown members fail closed with KeyError.
ALLOCATION_DISPOSITIONS: dict[AllocationFailureReason, AttemptDisposition] = {
    AllocationFailureReason.INSUFFICIENT_FREE_MEMORY: AttemptDisposition.INFRASTRUCTURE_WAIT,
    AllocationFailureReason.INSUFFICIENT_RESERVED_MEMORY: AttemptDisposition.INFRASTRUCTURE_WAIT,
    AllocationFailureReason.SERVER_NOT_PROVISIONED: AttemptDisposition.INFRASTRUCTURE_WAIT,
    AllocationFailureReason.IMPOSSIBLE_CAPACITY: AttemptDisposition.OPERATOR_REVIEW,
    AllocationFailureReason.NO_FRESH_METRICS: AttemptDisposition.TECHNICAL_FAILURE,
}

#: Platform dispositions never terminate a story.
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
    """Prefer an allocation refusal over a simultaneous product failure."""
    if allocation_failure is not None:
        return ALLOCATION_DISPOSITIONS[allocation_failure]
    if product_failure:
        return AttemptDisposition.PRODUCT_FAILURE
    return AttemptDisposition.NONE


def may_terminate_story(disposition: AttemptDisposition) -> bool:
    """Only product failures may terminate a story."""
    return disposition is AttemptDisposition.PRODUCT_FAILURE


class PlacementPath(StrEnum):
    """The two places a placement attempt can be routed from."""

    ENGINEERING = "engineering"
    DEPLOY = "deploy"


class RefusalRouting(StrEnum):
    """Path-specific handling for one disposition."""

    PARK_WAITING_RESOURCES = "park_waiting_resources"
    WAIT_FOR_ADMISSIBLE_TARGET = "wait_for_admissible_target"
    HUMAN_REVIEW_WITH_OWNER_NOTICE = "human_review_with_owner_notice"
    HUMAN_REVIEW_PLATFORM_ALERT = "human_review_platform_alert"
    CALLER_FAILURE_ROUTING = "caller_failure_routing"
    NO_REFUSAL = "no_refusal"


#: Total, injective disposition routing for each placement path.
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
    """Return the required routing behaviour; incomplete tables fail closed."""
    return REFUSAL_ROUTING[path][disposition]
