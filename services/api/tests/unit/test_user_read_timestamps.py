"""UserRead serialises user timestamps as UTC instants."""

from datetime import UTC, datetime, timedelta, timezone

from shared.models.user import User
from src.schemas.user import UserRead

STORED = datetime(2026, 8, 27, 21, 33, 3, 102772)


def _user(created_at: datetime, last_seen: datetime) -> User:
    return User(id=7, telegram_id=42, is_admin=False, created_at=created_at, last_seen=last_seen)


def test_naive_stored_utc_timestamps_serialise_with_utc_designator():
    body = UserRead.model_validate(_user(STORED, STORED)).model_dump(mode="json")

    assert body["last_seen"] == "2026-08-27T21:33:03.102772Z"
    assert body["created_at"] == "2026-08-27T21:33:03.102772Z"


def test_aware_timestamp_keeps_its_instant():
    berlin = timezone(timedelta(hours=2))
    aware = datetime(2026, 8, 27, 23, 33, 3, tzinfo=berlin)

    read = UserRead.model_validate(_user(aware, aware))

    assert read.last_seen == aware
    assert read.last_seen.astimezone(UTC) == datetime(2026, 8, 27, 21, 33, 3, tzinfo=UTC)
