"""Settlement of an exact PO obligation, called under the source row lock."""

from datetime import UTC, datetime

from fastapi import HTTPException

from shared.contracts.dto.owner_notification import (
    OwnerNoticeSettlement,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.vocab import OwnerNotificationEvent


def settle_notice(
    record: OwnerNotification, command: OwnerNoticeSettlement, *, current_owed_at: datetime
) -> OwnerNotification:
    if record.owed_at != command.owed_at:
        raise HTTPException(409, "Owner notice was replaced; read the current notice")
    if command.owed_at != current_owed_at and not command.resolve_deferred:
        raise HTTPException(409, "Owner notice was replaced; publication cannot settle it")
    if command.resolve_deferred and record.told_state != "suppressed":
        raise HTTPException(409, "Only a deferred notice can be resolved")
    if command.told_state == "suppressed":
        if record.event == OwnerNotificationEvent.STORY_WAITING_USER_SECRET:
            raise HTTPException(409, "Only the user can supply this secret; tell the request")
        if record.state != OwnerNotificationState.DELIVERED or record.told_state is not None:
            raise HTTPException(409, "No delivered, unsettled notice; read the current notice")
    elif command.told_state == "closed" and record.told_state != "suppressed":
        raise HTTPException(409, "Only a deferred notice can be closed")
    elif command.told_state == "told" and record.told_state in {"told", "closed"}:
        raise HTTPException(409, "Notice already settled")
    now = datetime.now(UTC)
    fields = {"told_state": command.told_state}
    if command.told_state == "suppressed":
        fields.update(
            suppressed_reason=command.reason, suppressed_by=command.suppressed_by, suppressed_at=now
        )
    elif command.told_state == "closed":
        fields.update(closed_at=now, closed_reason=command.reason)
    else:
        # A consumer may outrun the seam's DELIVERED write after po:input accepts
        # the event. This independent fact must survive that delivery write.
        fields["told_at"] = now
    return record.model_copy(update=fields)


_PO_FIELDS = (
    "told_state",
    "told_at",
    "suppressed_reason",
    "suppressed_by",
    "suppressed_at",
    "closed_at",
    "closed_reason",
    "deferred",
)


def preserve_po_settlement(stored: dict | None, incoming: OwnerNotification) -> OwnerNotification:
    """Delivery writes cannot erase a concurrent PO decision or a deferred obligation."""
    if stored is None:
        return incoming
    previous = OwnerNotification.model_validate(stored)
    if previous.owed_at == incoming.owed_at:
        fields = {key: getattr(previous, key) for key in _PO_FIELDS}
        if previous.suppressed_at != incoming.suppressed_at:
            # The delivery attempt may have read before suppression added its
            # administrator audience. It cannot settle or erase that copy.
            fields.update(
                {
                    key: getattr(previous, key)
                    for key in (
                        "admin_text",
                        "admin_state",
                        "admin_attempts",
                        "admin_detail",
                    )
                }
            )
        return incoming.model_copy(update=fields)
    deferred = list(previous.deferred)
    if previous.told_state == "suppressed":
        deferred.append(previous.model_copy(update={"deferred": []}))
    fields = {"deferred": deferred}
    if (previous.suppressed_at is not None or previous.deferred) and previous.admin_owed:
        fields.update(
            admin_text="\n\n".join(filter(None, [previous.admin_text, incoming.admin_text])),
            admin_state=OwnerNotificationState.OWED,
            admin_attempts=0,
            admin_detail=None,
        )
    return incoming.model_copy(update=fields)
