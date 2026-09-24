"""The expire-state-wait command: one consistent ending, and the comparison it carries."""

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
import pytest

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.state_wait import (
    StateWaitAnchor,
    StateWaitEnding,
    StateWaitExpiryCommand,
    StateWaitExpiryReason,
    StateWaitObservation,
    StateWaitSkipReason,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.vocab import OwnerNotificationEvent

DELIVERED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _record(event, terminal_status, **fields) -> OwnerNotification:
    return OwnerNotification(
        event=event,
        text="text",
        story_id="story-1",
        project_id="00000000-0000-0000-0000-000000000001",
        terminal_status=terminal_status,
        state=fields.pop("state", OwnerNotificationState.OWED),
        owed_at=datetime.now(UTC),
        **fields,
    )


def _command(status: StoryStatus, ending: StateWaitEnding, **anchor) -> StateWaitExpiryCommand:
    terminal = (
        StoryStatus.FAILED if ending is StateWaitEnding.FAIL else StoryStatus.WAITING_HUMAN_REVIEW
    )
    event = (
        OwnerNotificationEvent.STORY_FAILED
        if ending is StateWaitEnding.FAIL
        else OwnerNotificationEvent.STORY_BLOCKED
    )
    return StateWaitExpiryCommand(
        expected_status=status,
        ending=ending,
        anchor=StateWaitAnchor(**anchor),
        reason=StateWaitExpiryReason(
            status=status,
            waiting_on="deploy",
            config_key="supervisor.x",
            threshold_minutes=30,
            anchor="anchor",
            anchor_at=DELIVERED_AT.isoformat(),
            age_minutes=31.0,
            ending=ending,
        ),
        owner_notification=_record(event, terminal),
    )


def _ask(**fields) -> OwnerNotification:
    fields.setdefault("state", OwnerNotificationState.DELIVERED)
    fields.setdefault("delivered_at", DELIVERED_AT)
    return _record(
        OwnerNotificationEvent.STORY_WAITING_USER_SECRET, StoryStatus.WAITING_USER_SECRET, **fields
    )


def test_a_command_must_name_the_anchor_of_its_status():
    with pytest.raises(ValidationError, match="run_id"):
        _command(StoryStatus.DEPLOYING, StateWaitEnding.PARK, pr_number=42)
    with pytest.raises(ValidationError, match="pr_number"):
        _command(StoryStatus.PR_REVIEW, StateWaitEnding.PARK, run_id="deploy-1")
    with pytest.raises(ValidationError, match="delivered_at"):
        _command(StoryStatus.WAITING_USER_SECRET, StateWaitEnding.FAIL, run_id="deploy-1")
    with pytest.raises(ValidationError, match="story's updated_at"):
        _command(StoryStatus.IN_PROGRESS, StateWaitEnding.PARK, run_id="deploy-1")
    with pytest.raises(ValidationError, match="not a bounded wait"):
        _command(StoryStatus.CREATED, StateWaitEnding.PARK, run_id="deploy-1")


def test_a_command_owes_exactly_the_notice_of_its_ending():
    command = _command(StoryStatus.DEPLOYING, StateWaitEnding.PARK, run_id="deploy-1")
    wrong = command.owner_notification.model_copy(
        update={"event": OwnerNotificationEvent.STORY_FAILED}
    )
    with pytest.raises(ValidationError, match="owed notice"):
        StateWaitExpiryCommand.model_validate(
            {**command.model_dump(), "owner_notification": wrong.model_dump()}
        )


def test_a_run_wait_still_in_flight_on_its_run_matches():
    command = _command(StoryStatus.TESTING, StateWaitEnding.PARK, run_id="qa-1")
    seen = StateWaitObservation(
        status=StoryStatus.TESTING, run_id="qa-1", run_status=RunStatus.RUNNING
    )
    assert command.mismatch(seen) is None


@pytest.mark.parametrize(
    ("seen", "reason"),
    [
        (
            StateWaitObservation(
                status=StoryStatus.TESTING, run_id="deploy-1", run_status=RunStatus.RUNNING
            ),
            StateWaitSkipReason.STATUS_MOVED,
        ),
        (
            StateWaitObservation(
                status=StoryStatus.DEPLOYING, run_id="deploy-2", run_status=RunStatus.QUEUED
            ),
            StateWaitSkipReason.RUN_REPLACED,
        ),
        (
            StateWaitObservation(status=StoryStatus.DEPLOYING),
            StateWaitSkipReason.RUN_REPLACED,
        ),
        (
            StateWaitObservation(
                status=StoryStatus.DEPLOYING, run_id="deploy-1", run_status=RunStatus.FAILED
            ),
            StateWaitSkipReason.RUN_TERMINAL,
        ),
    ],
)
def test_a_deploy_wait_that_moved_names_how(seen, reason):
    command = _command(StoryStatus.DEPLOYING, StateWaitEnding.PARK, run_id="deploy-1")
    assert command.mismatch(seen).reason is reason


@pytest.mark.parametrize(
    ("ask", "secrets_saved", "reason"),
    [
        (_ask(), False, None),
        (None, False, StateWaitSkipReason.ASK_RECORD_CHANGED),
        (_ask(delivered_at=DELIVERED_AT + timedelta(seconds=1)), False, "changed"),
        (_ask(state=OwnerNotificationState.ABANDONED, delivered_at=None), False, "changed"),
        (_ask(), True, StateWaitSkipReason.SECRETS_SAVED),
    ],
)
def test_a_secret_wait_is_anchored_on_its_delivered_ask(ask, secrets_saved, reason):
    command = _command(
        StoryStatus.WAITING_USER_SECRET,
        StateWaitEnding.FAIL,
        run_id="deploy-1",
        ask_delivered_at=DELIVERED_AT,
    )
    seen = StateWaitObservation(
        status=StoryStatus.WAITING_USER_SECRET,
        run_id="deploy-1",
        run_status=RunStatus.COMPLETED,
        ask=ask,
        secrets_saved=secrets_saved,
    )
    skip = command.mismatch(seen)
    expected = StateWaitSkipReason.ASK_RECORD_CHANGED if reason == "changed" else reason
    assert (None if skip is None else skip.reason) is expected


def test_a_pull_request_wait_is_anchored_on_its_number():
    command = _command(StoryStatus.PR_REVIEW, StateWaitEnding.PARK, pr_number=42)
    same = StateWaitObservation(status=StoryStatus.PR_REVIEW, pr_number=42)
    other = StateWaitObservation(status=StoryStatus.PR_REVIEW, pr_number=43)
    assert command.mismatch(same) is None
    skip = command.mismatch(other)
    assert (skip.reason, skip.expected, skip.actual) == (
        StateWaitSkipReason.PR_NUMBER_CHANGED,
        "42",
        "43",
    )


def test_a_repeat_is_recognised_by_its_wait_not_its_age():
    command = _command(StoryStatus.DEPLOYING, StateWaitEnding.PARK, run_id="deploy-1")
    stored = {**command.reason.model_dump(mode="json"), "age_minutes": 95.5}
    assert command.is_repeat(StoryStatus.WAITING_HUMAN_REVIEW.value, stored)
    assert not command.is_repeat(StoryStatus.DEPLOYING.value, stored)
    moved_anchor = {**stored, "anchor_at": "2026-09-21T00:00:00+00:00"}
    assert not command.is_repeat(StoryStatus.WAITING_HUMAN_REVIEW.value, moved_anchor)
    assert not command.is_repeat(StoryStatus.WAITING_HUMAN_REVIEW.value, None)


def test_a_planless_in_progress_wait_is_anchored_on_the_story_row_and_its_tasks():
    anchored_at = DELIVERED_AT
    command = _command(StoryStatus.IN_PROGRESS, StateWaitEnding.PARK, story_updated_at=anchored_at)
    still = StateWaitObservation(
        status=StoryStatus.IN_PROGRESS, story_updated_at=anchored_at, work_cycle_tasks=0
    )
    assert command.mismatch(still) is None

    written = still.model_copy(update={"story_updated_at": anchored_at + timedelta(seconds=1)})
    assert command.mismatch(written).reason is StateWaitSkipReason.STORY_UPDATED

    planned = still.model_copy(update={"work_cycle_tasks": 2})
    skip = command.mismatch(planned)
    assert skip.reason is StateWaitSkipReason.TASKS_CREATED
    assert skip.actual == "2"

    moved_on = still.model_copy(update={"status": StoryStatus.PR_REVIEW})
    assert command.mismatch(moved_on).reason is StateWaitSkipReason.STATUS_MOVED
