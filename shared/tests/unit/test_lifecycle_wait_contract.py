"""The lifecycle-wait notices are durable owner notifications, checked against their task."""

from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
import pytest

from shared.contracts.dto.lifecycle_wait import (
    RESOURCE_WAIT_TASK_STATUSES,
    RESOURCES_RESUMED_TASK_STATUSES,
    TaskResourceWaitCommand,
)
from shared.contracts.dto.owner_notification import OwnerNotification
from shared.contracts.dto.task import TaskStatus
from shared.contracts.vocab import NON_DURABLE_OWNER_EVENTS, OwnerNotificationEvent

_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

#: The four non-terminal lifecycle events this contract makes durable.
LIFECYCLE_EVENTS = (
    OwnerNotificationEvent.TASK_WAITING_RESOURCES,
    OwnerNotificationEvent.TASK_WAITING_INFRASTRUCTURE,
    OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
    OwnerNotificationEvent.STORY_WAITING_USER_SECRET,
)


def _record(**update) -> OwnerNotification:
    fields = {
        "event": "task_waiting_resources",
        "text": "Engineering is waiting for server capacity.",
        "story_id": "story-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "terminal_status": "in_progress",
        "task_id": "task-1",
        "expected_task_statuses": ["waiting_resources"],
        "state": "owed",
        "owed_at": _NOW.isoformat(),
    }
    return OwnerNotification.model_validate({**fields, **update})


@pytest.mark.parametrize("event", LIFECYCLE_EVENTS, ids=str)
def test_every_lifecycle_event_is_an_owed_owner_notification(event) -> None:
    assert event not in NON_DURABLE_OWNER_EVENTS
    record = _record(event=event.value)

    assert record.event is event
    assert record.owed


@pytest.mark.parametrize("event", sorted(NON_DURABLE_OWNER_EVENTS), ids=str)
def test_progress_notices_stay_refused(event) -> None:
    with pytest.raises(ValidationError, match="never an owed owner notification"):
        _record(event=event.value)


def test_a_record_written_before_the_task_expectation_existed_reads_as_none() -> None:
    stored = _record().model_dump(mode="json")
    del stored["expected_task_statuses"]
    del stored["task_id"]

    assert OwnerNotification.model_validate(stored).expected_task_statuses is None


def test_a_task_expectation_needs_its_task_and_a_status() -> None:
    with pytest.raises(ValidationError, match="needs a task_id"):
        _record(task_id=None)
    with pytest.raises(ValidationError, match="needs a task_id"):
        _record(expected_task_statuses=[])


def test_the_task_expectation_round_trips_through_json() -> None:
    record = _record(expected_task_statuses=[s.value for s in RESOURCES_RESUMED_TASK_STATUSES])

    again = OwnerNotification.model_validate(record.model_dump(mode="json"))

    assert again.expected_task_statuses == (TaskStatus.TODO, TaskStatus.IN_DEV)
    assert RESOURCE_WAIT_TASK_STATUSES == (TaskStatus.WAITING_RESOURCES,)


def test_a_write_naming_a_replaced_obligation_is_superseded() -> None:
    """A later notice on the same Run replaces the earlier one; the earlier one's
    visit may not write it back, stamped or not."""
    waiting = _record(owed_at=_NOW.isoformat(), last_attempt_at=_NOW.isoformat())
    resumed = _record(
        event="task_resources_resumed",
        owed_at=(_NOW + timedelta(minutes=5)).isoformat(),
        expected_task_statuses=["todo", "in_dev"],
    )

    assert resumed.supersedes(waiting.model_copy(update={"state": "delivered"}))
    assert resumed.supersedes(waiting.model_copy(update={"last_attempt_at": None}))
    assert not waiting.supersedes(resumed)


def test_a_park_command_announces_only_a_resource_wait() -> None:
    fields = {
        "run_id": "eng-1",
        "allocation_failure_reason": "insufficient_free_memory",
        "text": "Engineering is waiting.",
        "actor": "supervisor",
    }
    for event in ("task_waiting_resources", "task_waiting_infrastructure"):
        assert TaskResourceWaitCommand.model_validate({**fields, "event": event}).event == event
    with pytest.raises(ValidationError, match="does not announce a resource wait"):
        TaskResourceWaitCommand.model_validate({**fields, "event": "task_resources_resumed"})
