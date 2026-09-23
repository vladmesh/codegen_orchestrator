"""The owner-notification attempt claim and the fence behind it.

Whether a delivery attempt may be made now, and the stamp that says one is being
made, are one decision taken on a locked row: the callers that attempt — the
routing that owed the record and the recovery sweep — are separate code paths
that may run in any order or at the same moment, and a read in one of them
followed by a write would let both read "never attempted" and both publish.

The claim stamps ``last_attempt_at`` and hands the caller the stamped record.
Every write that attempt makes carries that stamp, which is what the fence
checks: a visit that outlived its claim, while another caller claimed the record
again, carries an older stamp than the record and may not overwrite what the
newer visit settled.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status

from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED,
    OwnerNotification,
    OwnerNotificationAttemptClaim,
)


def claim_attempt(stored: dict | None) -> tuple[OwnerNotificationAttemptClaim, dict | None]:
    """Decide one attempt on a record read under its row lock.

    Returns the answer and, when it is granted, the stamped record to store in
    the same transaction. A refusal changes nothing.
    """
    if stored is None:
        return OwnerNotificationAttemptClaim(granted=False, notification=None), None
    record = OwnerNotification.model_validate(stored)
    now = datetime.now(UTC)
    if not record.attempt_due(now):
        return OwnerNotificationAttemptClaim(granted=False, notification=record), None
    stamped = record.model_copy(update={"last_attempt_at": now})
    return (
        OwnerNotificationAttemptClaim(granted=True, notification=stamped),
        stamped.model_dump(mode="json"),
    )


def refuse_superseded_write(stored: dict | None, incoming: OwnerNotification) -> None:
    """Refuse a write from an attempt older than the one the record holds."""
    if stored is None:
        return
    if OwnerNotification.model_validate(stored).supersedes(incoming):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": OWNER_NOTIFICATION_ATTEMPT_SUPERSEDED,
                "message": "A newer delivery attempt holds this owner notification",
            },
        )
