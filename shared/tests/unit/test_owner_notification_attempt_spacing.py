"""The last-attempt stamp: absent means never attempted, and it spaces every audience."""

from datetime import UTC, datetime, timedelta

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_ATTEMPT_INTERVAL,
    OwnerNotification,
)

_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)

#: An owed record exactly as production stored it before the stamp existed.
_PRE_STAMP = {
    "event": "story_completed",
    "text": "The story is finished.",
    "story_id": "story-1",
    "project_id": "00000000-0000-0000-0000-000000000001",
    "terminal_status": "completed",
    "state": "owed",
    "owed_at": (_NOW - timedelta(days=3)).isoformat(),
    "attempts": 1,
    "detail": "ConnectionError: po:input is unreachable",
}


def _record(**update) -> OwnerNotification:
    return OwnerNotification.model_validate({**_PRE_STAMP, **update})


def test_a_record_written_before_the_stamp_existed_is_never_attempted_and_due() -> None:
    record = OwnerNotification.model_validate(_PRE_STAMP)

    assert record.last_attempt_at is None
    assert record.attempt_due(_NOW)


def test_an_attempt_is_due_only_a_full_interval_after_the_last() -> None:
    assert not _record(last_attempt_at=_NOW).attempt_due(_NOW)
    almost = _NOW - OWNER_NOTIFICATION_ATTEMPT_INTERVAL + timedelta(microseconds=1)
    assert not _record(last_attempt_at=almost).attempt_due(_NOW)
    assert _record(last_attempt_at=_NOW - OWNER_NOTIFICATION_ATTEMPT_INTERVAL).attempt_due(_NOW)


def test_the_one_stamp_spaces_the_administrator_audience_too() -> None:
    admin_only = _record(
        state="delivered", admin_text="Parked.", admin_state="owed", last_attempt_at=_NOW
    )

    assert not admin_only.attempt_due(_NOW)
    assert admin_only.attempt_due(_NOW + OWNER_NOTIFICATION_ATTEMPT_INTERVAL)


def test_a_settled_record_is_never_due() -> None:
    for state in ("delivered", "unaddressable", "abandoned", "voided"):
        assert not _record(state=state).attempt_due(_NOW)


def test_a_write_from_an_older_attempt_is_superseded() -> None:
    held = _record(last_attempt_at=_NOW)

    assert held.supersedes(_record(last_attempt_at=_NOW - timedelta(seconds=1)))
    assert held.supersedes(_record())
    assert not held.supersedes(_record(last_attempt_at=_NOW, state="delivered"))
    assert not _record().supersedes(_record())


def test_a_record_owed_afresh_is_a_new_obligation_not_a_stale_write() -> None:
    held = _record(state="voided", last_attempt_at=_NOW)

    assert not held.supersedes(_record(owed_at=_NOW, attempts=0, detail=None))
