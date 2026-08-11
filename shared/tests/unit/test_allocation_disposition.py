"""The invariant itself: what a failed placement attempt may do to a story."""

import pytest

from shared.allocation_disposition import (
    ALLOCATION_DISPOSITIONS,
    INFRASTRUCTURE_DISPOSITIONS,
    AttemptDisposition,
    attempt_disposition,
    may_terminate_story,
)
from shared.contracts.dto.run_result import AllocationFailureReason


@pytest.mark.parametrize("reason", list(AllocationFailureReason), ids=lambda r: r.value)
def test_no_allocation_refusal_may_terminate_a_story(reason):
    """Every allocation reason describes the platform, never the user's project."""
    disposition = attempt_disposition(reason, product_failure=True)

    assert disposition in INFRASTRUCTURE_DISPOSITIONS
    assert not may_terminate_story(disposition)


def test_every_reason_is_classified_explicitly():
    """A new reason nobody classified must not default into the product path."""
    assert set(ALLOCATION_DISPOSITIONS) == set(AllocationFailureReason)


def test_allocation_refusal_outranks_a_product_failure_in_the_same_attempt():
    """The priority rule, stated once so two callers cannot order it differently."""
    both = attempt_disposition(AllocationFailureReason.SERVER_NOT_PROVISIONED, product_failure=True)
    alone = attempt_disposition(
        AllocationFailureReason.SERVER_NOT_PROVISIONED, product_failure=False
    )

    assert both is AttemptDisposition.INFRASTRUCTURE_WAIT
    assert alone is AttemptDisposition.INFRASTRUCTURE_WAIT


def test_a_failure_without_an_allocation_refusal_stays_the_callers_own():
    assert attempt_disposition(None, product_failure=True) is AttemptDisposition.PRODUCT_FAILURE
    assert may_terminate_story(AttemptDisposition.PRODUCT_FAILURE)
    assert attempt_disposition(None, product_failure=False) is AttemptDisposition.NONE


def test_unprovisioned_and_capacity_refusals_are_both_waits():
    """The wait is not a capacity concept: an unfinished host waits the same way."""
    assert (
        attempt_disposition(AllocationFailureReason.SERVER_NOT_PROVISIONED, product_failure=False)
        is AttemptDisposition.INFRASTRUCTURE_WAIT
    )
    assert (
        attempt_disposition(AllocationFailureReason.INSUFFICIENT_FREE_MEMORY, product_failure=False)
        is AttemptDisposition.INFRASTRUCTURE_WAIT
    )


def test_impossible_capacity_needs_an_operator_but_is_still_not_the_project():
    disposition = attempt_disposition(
        AllocationFailureReason.IMPOSSIBLE_CAPACITY, product_failure=True
    )

    assert disposition is AttemptDisposition.OPERATOR_REVIEW
    assert not may_terminate_story(disposition)
