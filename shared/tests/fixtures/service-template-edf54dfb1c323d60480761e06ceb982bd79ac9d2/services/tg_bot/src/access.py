"""Telegram identity validation and backend-status access predicates."""

from __future__ import annotations

from typing import Final

TELEGRAM_CHANNEL: Final[str] = "telegram"
ACTIVE_STATUS: Final[str] = "active"


def telegram_external_id(telegram_id: int | None) -> str | None:
    """Return a valid Telegram external identity, or ``None`` when malformed."""

    if isinstance(telegram_id, bool) or not isinstance(telegram_id, int) or telegram_id <= 0:
        return None
    return str(telegram_id)


def is_active(status: str | None) -> bool:
    """The backend's active status is the only admission decision."""

    return status == ACTIVE_STATUS
