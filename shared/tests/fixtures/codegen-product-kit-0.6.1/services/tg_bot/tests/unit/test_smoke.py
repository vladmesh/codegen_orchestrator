"""Smoke tests: verify that key modules are importable.

Catches relative-import bugs and missing dependencies that would crash
the container at startup but pass unit tests (which use absolute imports).
"""

from __future__ import annotations


def test_main_importable():
    from services.tg_bot.src.main import build_application

    assert build_application is not None


def test_middleware_importable():
    from services.tg_bot.src.middleware import install_update_logging

    assert install_update_logging is not None


def test_placeholder_token_detected(monkeypatch):
    from services.tg_bot.src.main import PLACEHOLDER_TELEGRAM_BOT_TOKEN, _uses_placeholder_token

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PLACEHOLDER_TELEGRAM_BOT_TOKEN)

    assert _uses_placeholder_token()


def test_placeholder_token_requires_dev_opt_in(monkeypatch):
    from services.tg_bot.src.main import (
        ALLOW_PLACEHOLDER_TOKEN_ENV,
        PLACEHOLDER_TELEGRAM_BOT_TOKEN,
        _allows_placeholder_token,
    )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PLACEHOLDER_TELEGRAM_BOT_TOKEN)
    monkeypatch.delenv(ALLOW_PLACEHOLDER_TOKEN_ENV, raising=False)

    assert not _allows_placeholder_token()


def test_placeholder_token_can_be_allowed_for_dev(monkeypatch):
    from services.tg_bot.src.main import (
        ALLOW_PLACEHOLDER_TOKEN_ENV,
        PLACEHOLDER_TELEGRAM_BOT_TOKEN,
        _allows_placeholder_token,
    )

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", PLACEHOLDER_TELEGRAM_BOT_TOKEN)
    monkeypatch.setenv(ALLOW_PLACEHOLDER_TOKEN_ENV, "true")

    assert _allows_placeholder_token()
