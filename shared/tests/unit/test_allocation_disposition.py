"""The invariant itself: what a failed placement attempt may do to a story."""

import pytest

from shared.allocation_disposition import (
    ALLOCATION_DISPOSITIONS,
    INFRASTRUCTURE_DISPOSITIONS,
    REFUSAL_ROUTING,
    AttemptDisposition,
    PlacementPath,
    attempt_disposition,
    may_terminate_story,
    refusal_routing,
)
from shared.contracts.dto.run_result import AllocationFailureReason
from shared.tests.allocation_routing_cases import REFUSAL_ROUTING_CASES


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


@pytest.mark.parametrize("path", list(PlacementPath), ids=lambda p: p.value)
def test_every_path_names_every_disposition(path):
    """No routing decision may fall through to a default on either path."""
    assert set(REFUSAL_ROUTING[path]) == set(AttemptDisposition)


@pytest.mark.parametrize("path", list(PlacementPath), ids=lambda p: p.value)
def test_no_path_answers_two_dispositions_the_same_way(path):
    """The collapse itself: two dispositions sharing one behaviour deletes one.

    The deploy path used to record every refusal as the same infrastructure
    wait, which is how a request no server could ever fit polled forever with
    nobody told. This fails the moment that returns, on either path.
    """
    behaviours = list(REFUSAL_ROUTING[path].values())

    assert len(set(behaviours)) == len(behaviours)


@pytest.mark.parametrize("case", REFUSAL_ROUTING_CASES, ids=lambda c: c.reason.value)
def test_production_table_matches_the_expected_behaviour_matrix(case):
    """The routing the services are tested against is the routing they get.

    The matrix in `shared/tests/allocation_routing_cases.py` is written by hand,
    so changing `REFUSAL_ROUTING` without deciding what each path now does fails
    here rather than passing silently against a derived expectation.
    """
    assert attempt_disposition(case.reason, product_failure=True) is case.disposition
    assert refusal_routing(PlacementPath.ENGINEERING, case.disposition) is case.engineering.routing
    assert refusal_routing(PlacementPath.DEPLOY, case.disposition) is case.deploy.routing


def test_every_allocation_reason_has_a_case():
    assert {case.reason for case in REFUSAL_ROUTING_CASES} == set(AllocationFailureReason)


@pytest.mark.parametrize("path_attr", ["engineering", "deploy"])
def test_expected_behaviours_are_distinct_per_path(path_attr):
    """Distinct dispositions must expect distinct observable behaviour.

    Without this the matrix could itself describe a collapse — which is exactly
    how the previous suite passed while every deploy refusal waited forever.
    """
    by_disposition = {case.disposition: getattr(case, path_attr) for case in REFUSAL_ROUTING_CASES}
    expectations = list(by_disposition.values())

    assert len(set(expectations)) == len(expectations)
