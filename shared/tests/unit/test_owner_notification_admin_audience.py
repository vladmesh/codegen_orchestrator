"""The administrator audience is additive: released records keep their owner meaning."""

from datetime import UTC, datetime

from pydantic import ValidationError
import pytest

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState

_RELEASED = {
    "event": "story_completed",
    "text": "The story is finished.",
    "story_id": "story-1",
    "project_id": "00000000-0000-0000-0000-000000000001",
    "terminal_status": "completed",
    "state": "delivered",
    "owed_at": datetime.now(UTC).isoformat(),
    "attempts": 1,
    "detail": None,
}


def test_a_released_record_owes_no_administrator_and_keeps_its_owner_state() -> None:
    record = OwnerNotification.model_validate(_RELEASED)

    assert record.state is OwnerNotificationState.DELIVERED
    assert not record.owed
    assert (record.admin_text, record.admin_state, record.admin_attempts) == (None, None, 0)
    assert not record.admin_owed


def test_an_owed_administrator_audience_is_independent_of_the_owner() -> None:
    record = OwnerNotification.model_validate(
        {**_RELEASED, "admin_text": "Parked.", "admin_state": "owed"}
    )

    assert not record.owed
    assert record.admin_owed


@pytest.mark.parametrize("partial", [{"admin_text": "Parked."}, {"admin_state": "owed"}])
def test_an_administrator_audience_is_whole_or_absent(partial: dict) -> None:
    with pytest.raises(ValidationError):
        OwnerNotification.model_validate({**_RELEASED, **partial})
