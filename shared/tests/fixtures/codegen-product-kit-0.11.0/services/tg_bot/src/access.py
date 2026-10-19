"""Telegram identity validation and backend-status access predicates."""

from __future__ import annotations

from typing import Final

TELEGRAM_CHANNEL: Final[str] = "telegram"
ACTIVE_STATUS: Final[str] = "active"
# The backend core accepts a user identity on package routes only through these headers.
IDENTITY_CAPABILITY_HEADER: Final[str] = "X-Identity-Capability"
USER_CHANNEL_HEADER: Final[str] = "X-User-Channel"
USER_EXTERNAL_ID_HEADER: Final[str] = "X-User-External-Id"


def telegram_external_id(telegram_id: int | None) -> str | None:
    """Return a valid Telegram external identity, or ``None`` when malformed."""

    if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
        return None
    return str(telegram_id)


def is_active(status: str | None) -> bool:
    """The backend's active status is the only admission decision."""

    return status == ACTIVE_STATUS
