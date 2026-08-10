"""Fail closed on the ambiguous ``user_id`` recipient field.

``user_id`` used to mean a Telegram chat id when the bot produced it and an
internal ``User.id`` when the scheduler did. The field is gone: the destination
is ``telegram_chat_id`` and the internal id travels as ``owner_user_id``.

Pydantic accepts unknown fields by default, so a payload published before the
rename would validate against the new models with its recipient silently
dropped — work with nobody to report back to, and no sign that a recipient was
ever supplied. That is worse than either keeping or removing the field, so every
addressable contract rejects ``user_id`` outright: the message fails validation,
the consumer logs it and raises an admin alert naming the story, project and
event instead of doing unaddressable work.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator
import structlog

logger = structlog.get_logger(__name__)

# The removed field. Named once so the rejection, the alert and the tests that
# pin them all speak about the same string.
LEGACY_RECIPIENT_FIELD = "user_id"

# Identifiers an alert needs to find the work a rejected message belonged to,
# mapped to the log keys the rest of the project uses (``event`` is structlog's
# own name for the log event, so a payload's event travels as ``po_event``).
_ALERT_IDENTIFIER_FIELDS = {
    "event": "po_event",
    "story_id": "story_id",
    "project_id": "project_id",
    "task_id": "task_id",
}


class LegacyRecipientFieldError(ValueError):
    """A queue payload still carries the removed ambiguous ``user_id`` field."""


def reject_legacy_recipient_field(data: Any) -> Any:
    """Raise if *data* carries the removed ``user_id`` field, else return it."""
    if isinstance(data, dict) and LEGACY_RECIPIENT_FIELD in data:
        raise LegacyRecipientFieldError(
            f"{LEGACY_RECIPIENT_FIELD!r} was removed because it meant both a Telegram "
            "chat id and an internal User.id; publish telegram_chat_id (destination) "
            "and owner_user_id (internal id) instead"
        )
    return data


def has_legacy_recipient_field(data: Any) -> bool:
    """Whether *data* is a payload carrying the removed ``user_id`` field."""
    return isinstance(data, dict) and LEGACY_RECIPIENT_FIELD in data


def legacy_recipient_identifiers(data: Any) -> dict[str, str]:
    """The story/project/event identifiers a rejected payload carries."""
    if not isinstance(data, dict):
        return {}
    return {
        log_key: str(data[field])
        for field, log_key in _ALERT_IDENTIFIER_FIELDS.items()
        if data.get(field) not in (None, "")
    }


class RejectsLegacyRecipientField(BaseModel):
    """Base for contracts that address a user: refuses the removed field."""

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_recipient_field(cls, data: Any) -> Any:
        return reject_legacy_recipient_field(data)


async def alert_legacy_recipient_field(*, source: str, entry_id: str, data: Any) -> None:
    """Log and alert admins that a legacy-addressed message was rejected.

    ``notify_admins_best_effort`` is imported here rather than at module import:
    it reaches for aiohttp and the internal API, and this module is imported by
    every service that touches a queue contract, including ones that ship
    neither.
    """
    from shared.notifications import notify_admins_best_effort

    identifiers = legacy_recipient_identifiers(data)
    logger.error(
        "legacy_recipient_field_rejected",
        source=source,
        entry_id=entry_id,
        legacy_field=LEGACY_RECIPIENT_FIELD,
        **identifiers,
    )
    named = " ".join(f"{key}={value}" for key, value in identifiers.items()) or "no identifiers"
    await notify_admins_best_effort(
        f"Rejected a message addressed by the removed {LEGACY_RECIPIENT_FIELD!r} field: "
        f"source={source} entry={entry_id} {named}",
        level="error",
        source=source,
        entry_id=entry_id,
        **identifiers,
    )
