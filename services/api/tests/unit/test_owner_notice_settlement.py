"""PO settlement is independent of delivery and fenced by the obligation identity."""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
import pytest

from shared.contracts.dto.owner_notification import OwnerNoticeSettlement, OwnerNotification
from src.owner_notification_settlement import preserve_po_settlement, settle_notice


def record(**fields):
    return OwnerNotification.model_validate(
        {
            "event": "story_blocked",
            "text": "Work stopped",
            "story_id": "story-1",
            "project_id": "project-1",
            "terminal_status": "waiting_human_review",
            "state": "delivered",
            "owed_at": datetime.now(UTC),
            **fields,
        }
    )


def command(notice, **fields):
    return OwnerNoticeSettlement.model_validate(
        {
            "source": "run",
            "source_id": "run-1",
            "owed_at": notice.owed_at,
            "told_state": "suppressed",
            "reason": "Small things can wait",
            "suppressed_by": "user",
            **fields,
        }
    )


def test_suppression_keeps_delivery_and_records_decision():
    notice = record()
    settled = settle_notice(notice, command(notice), current_owed_at=notice.owed_at)
    assert settled.state == "delivered"
    assert settled.told_state == "suppressed"
    assert settled.suppressed_by == "user"
    assert settled.suppressed_reason == "Small things can wait"
    assert settled.suppressed_at is not None


def test_stale_identity_is_refused():
    notice = record()
    with pytest.raises(HTTPException, match="409"):
        settle_notice(
            notice,
            command(notice, owed_at=notice.owed_at - timedelta(seconds=1)),
            current_owed_at=notice.owed_at,
        )


def test_secret_request_cannot_be_suppressed():
    notice = record(event="story_waiting_user_secret", terminal_status="waiting_user_secret")
    with pytest.raises(HTTPException, match="Only the user"):
        settle_notice(notice, command(notice), current_owed_at=notice.owed_at)


@pytest.mark.parametrize("outcome", ["told", "closed"])
def test_deferred_notice_can_be_explicitly_resolved(outcome):
    notice = record()
    deferred = settle_notice(notice, command(notice), current_owed_at=notice.owed_at)
    settled = settle_notice(
        deferred,
        command(notice, told_state=outcome, suppressed_by=None, resolve_deferred=True),
        current_owed_at=notice.owed_at,
    )
    assert settled.told_state == outcome
    assert getattr(settled, f"{outcome}_at") is not None


def test_old_records_are_unsettled():
    assert record().told_state is None


def test_late_delivery_preserves_po_decision_and_new_admin_audience():
    original = record()
    suppressed = settle_notice(
        original, command(original), current_owed_at=original.owed_at
    ).model_copy(
        update={
            "admin_text": "Deferred copy",
            "admin_state": "owed",
        }
    )
    merged = preserve_po_settlement(suppressed.model_dump(mode="json"), original)
    assert merged.told_state == "suppressed"
    assert merged.admin_text == "Deferred copy"
    assert merged.admin_state == "owed"


def test_replacement_retains_suppression_and_its_pending_admin_copy():
    original = record()
    suppressed = settle_notice(
        original, command(original), current_owed_at=original.owed_at
    ).model_copy(
        update={
            "admin_text": "Deferred copy",
            "admin_state": "owed",
        }
    )
    replacement = record(owed_at=original.owed_at + timedelta(seconds=1))
    merged = preserve_po_settlement(suppressed.model_dump(mode="json"), replacement)
    assert merged.told_state is None
    assert merged.deferred[0].owed_at == original.owed_at
    assert merged.deferred[0].suppressed_reason == suppressed.suppressed_reason
    assert merged.admin_text == "Deferred copy"
    again = preserve_po_settlement(merged.model_dump(mode="json"), record())
    assert again.admin_text == "Deferred copy"


@pytest.mark.parametrize("told_state", ["suppressed", "closed"])
def test_settlement_requires_nonblank_reason(told_state):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="non-empty reason"):
        command(
            record(),
            told_state=told_state,
            reason="  ",
            suppressed_by="po" if told_state == "suppressed" else None,
        )


def test_cannot_suppress_an_undelivered_or_already_settled_notice():
    for notice in (record(state="owed"), record(told_state="told", told_at=datetime.now(UTC))):
        with pytest.raises(HTTPException, match="unsettled"):
            settle_notice(notice, command(notice), current_owed_at=notice.owed_at)


def test_late_publication_cannot_settle_replaced_deferral_but_explicit_resolution_can():
    notice = record()
    deferred = settle_notice(notice, command(notice), current_owed_at=notice.owed_at)
    current = notice.owed_at + timedelta(seconds=1)
    publication = command(notice, told_state="told", suppressed_by=None)
    with pytest.raises(HTTPException, match="publication cannot settle"):
        settle_notice(deferred, publication, current_owed_at=current)
    resolution = command(notice, told_state="told", suppressed_by=None, resolve_deferred=True)
    assert settle_notice(deferred, resolution, current_owed_at=current).told_state == "told"
