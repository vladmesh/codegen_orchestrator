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
    operator_retry_record,
    planned_record,
    planning_is_due,
    planning_retry_delay,
    planning_retry_queued_key,
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
        planning_attempt_id=None,
        reopen=False,
        now=NOW + timedelta(minutes=1),
    )
    assert retrying.failed_attempts == 1
    assert planned.state is StoryPlanningState.PLANNED
    assert planned.channels == ["claude"]

    again = failed_record(planned, _failed(), max_retries=3, now=NOW)

    assert again.failed_attempts == 1


def test_an_operator_retry_is_owed_at_once_with_the_count_reset():
    parked = failed_record(None, _failed(retriable=False), max_retries=3, now=NOW)

    owed = operator_retry_record(parked, max_retries=3, now=NOW)

    assert owed.state is StoryPlanningState.RETRYING
    assert owed.failed_attempts == 0
    assert owed.next_attempt_at == NOW
    assert owed.last_failure.detail == parked.last_failure.detail
    assert planning_is_due(owed, NOW)
    # The count starts again: the next failure is the first of a fresh bound.
    assert failed_record(owed, _failed(), max_retries=3, now=NOW).failed_attempts == 1


def test_only_a_retry_before_its_time_is_not_due():
    retrying = failed_record(None, _failed(), max_retries=3, now=NOW)

    assert not planning_is_due(retrying, NOW)
    assert planning_is_due(retrying, retrying.next_attempt_at)
    assert planning_is_due(None, NOW)
    assert planning_is_due(
        failed_record(None, _failed(retriable=False), max_retries=3, now=NOW), NOW
    )


def test_every_retrying_record_has_its_own_publish_guard():
    first = failed_record(None, _failed(), max_retries=3, now=NOW)
    later = failed_record(None, _failed(), max_retries=3, now=NOW + timedelta(hours=1))

    assert planning_retry_queued_key("s-1", first) != planning_retry_queued_key("s-1", later)
    assert planning_retry_queued_key("s-1", first).startswith("story:planning_retry_queued:s-1:")
    with pytest.raises(ValueError):
        planning_retry_queued_key(
            "s-1", failed_record(None, _failed(retriable=False), max_retries=0, now=NOW)
        )
