"""Telegram identity normalization and status-only admission tests."""

from __future__ import annotations

from services.tg_bot.src.access import is_active, telegram_external_id


def test_active_status_is_admitted() -> None:
    assert is_active("active")


def test_inactive_or_unknown_status_is_denied() -> None:
    assert not is_active("inactive")
    assert not is_active(None)
    assert not is_active("unknown")


def test_malformed_telegram_id_is_denied_before_lookup() -> None:
    assert telegram_external_id(None) is None
    assert telegram_external_id(0) is None
    assert telegram_external_id(-1) is None
    assert telegram_external_id(True) is None


def test_valid_telegram_id_becomes_external_identity() -> None:
    assert telegram_external_id(123456789) == "123456789"
