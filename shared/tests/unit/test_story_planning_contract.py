"""The planning outcome a story carries, and the one retry-or-park decision over it."""

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
import pytest

from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.story_planning import (
    PLANNING_RETRY_BACKOFF_SECONDS,
    StoryPlanningOutcome,
    StoryPlanningReport,
    StoryPlanningState,
    failed_record,
    planned_record,
    planning_retry_delay,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _failure(detail: str = "RuntimeError: LLM timeout") -> StoryFailure:
    return StoryFailure(code=StoryFailureCode.PLANNING_FAILED, source="architect", detail=detail)


def _failed(*, retriable: bool = True) -> StoryPlanningReport:
    return StoryPlanningReport(
        outcome=StoryPlanningOutcome.FAILED,
        failure=_failure(),
        retriable=retriable,
        channel_failures=["codex:rate_limited"],
    )


def test_a_failed_report_carries_a_planning_failure_and_a_success_none():
    with pytest.raises(ValidationError, match="needs its failure"):
        StoryPlanningReport(outcome=StoryPlanningOutcome.FAILED)
    with pytest.raises(ValidationError, match="planning_failed"):
        StoryPlanningReport(
            outcome=StoryPlanningOutcome.FAILED,
            failure=StoryFailure(
                code=StoryFailureCode.SCAFFOLD_FAILED, source="architect", detail="d"
            ),
        )
    with pytest.raises(ValidationError, match="carries no failure"):
        StoryPlanningReport(outcome=StoryPlanningOutcome.SUCCEEDED, failure=_failure())


def test_the_backoff_doubles_from_the_first_retry():
    assert [planning_retry_delay(n).total_seconds() for n in (1, 2, 3)] == [
        PLANNING_RETRY_BACKOFF_SECONDS,
        PLANNING_RETRY_BACKOFF_SECONDS * 2,
        PLANNING_RETRY_BACKOFF_SECONDS * 4,
    ]


def test_retriable_failures_retry_up_to_the_bound_then_park():
    previous = None
    for attempt in (1, 2, 3):
        previous = failed_record(previous, _failed(), max_retries=3, now=NOW)
        assert previous.state is StoryPlanningState.RETRYING
        assert previous.failed_attempts == attempt
        assert previous.next_attempt_at == NOW + planning_retry_delay(attempt)
        assert previous.channel_failures == ["codex:rate_limited"]

    parked = failed_record(previous, _failed(), max_retries=3, now=NOW)

    assert parked.state is StoryPlanningState.PARKED
    assert parked.failed_attempts == 4
    assert parked.next_attempt_at is None
    assert parked.last_failure.detail == _failure().detail


def test_an_unretriable_failure_parks_at_once():
    parked = failed_record(None, _failed(retriable=False), max_retries=3, now=NOW)

    assert parked.state is StoryPlanningState.PARKED
    assert parked.failed_attempts == 1


def test_a_success_resets_the_count_for_the_next_failure():
    retrying = failed_record(None, _failed(), max_retries=3, now=NOW)
    planned = planned_record(
        StoryPlanningReport(outcome=StoryPlanningOutcome.SUCCEEDED, channels=["claude"]),
        NOW + timedelta(minutes=1),
    )
    assert retrying.failed_attempts == 1
    assert planned.state is StoryPlanningState.PLANNED
    assert planned.channels == ["claude"]

    again = failed_record(planned, _failed(), max_retries=3, now=NOW)

    assert again.failed_attempts == 1
