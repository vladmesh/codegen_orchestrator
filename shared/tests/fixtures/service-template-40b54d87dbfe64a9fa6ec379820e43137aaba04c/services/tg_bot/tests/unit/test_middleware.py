"""Tests for Telegram bot update logging middleware."""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationBuilder

from services.tg_bot.src.middleware import _extract_update_info, install_update_logging
from shared.logging import configure_logging

configure_logging(service_name="tg_bot_test")


@pytest.fixture()
def log_capture():
    """Capture structlog JSON output via a dedicated handler."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", key="timestamp"),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
            ],
        )
    )
    root = logging.getLogger()
    root.addHandler(handler)
    yield buf
    root.removeHandler(handler)


def _make_update(
    *,
    user_id: int = 42,
    text: str = "/start",
    update_id: int = 1,
) -> Update:
    """Build a minimal Update with a text message."""
    user = User(id=user_id, is_bot=False, first_name="Test")
    chat = Chat(id=user_id, type="private")
    message = Message(
        message_id=1,
        date=None,
        chat=chat,
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


def _make_callback_update(*, user_id: int = 42, data: str = "btn_ok") -> Update:
    """Build a minimal Update with a callback query."""
    user = User(id=user_id, is_bot=False, first_name="Test")
    callback_query = MagicMock()
    callback_query.data = data
    callback_query.from_user = user
    update = MagicMock(spec=Update)
    update.effective_user = user
    update.message = None
    update.callback_query = callback_query
    update.update_id = 2
    return update


class TestExtractUpdateInfo:
    def test_command_message(self) -> None:
        update = _make_update(text="/help arg1")
        user_id, update_type, command = _extract_update_info(update)

        assert user_id == "tg:42"
        assert update_type == "command"
        assert command == "/help"

    def test_regular_message(self) -> None:
        update = _make_update(text="hello there")
        user_id, update_type, command = _extract_update_info(update)

        assert user_id == "tg:42"
        assert update_type == "message"
        assert command is None

    def test_callback_query(self) -> None:
        update = _make_callback_update(data="action:confirm")
        user_id, update_type, command = _extract_update_info(update)

        assert user_id == "tg:42"
        assert update_type == "callback_query"
        assert command == "action:confirm"


class TestInstallUpdateLogging:
    @pytest.mark.asyncio
    @patch("telegram.Bot.initialize", new_callable=AsyncMock)
    @patch("telegram.Bot.get_me", new_callable=AsyncMock)
    async def test_update_logged_with_standard_fields(
        self, mock_get_me, mock_bot_init, log_capture
    ) -> None:
        app = ApplicationBuilder().token("fake:token").build()
        await app.initialize()

        async def _noop(update, context):
            pass

        from telegram.ext import TypeHandler

        app.add_handler(TypeHandler(Update, _noop))
        install_update_logging(app)

        update = _make_update(text="/start")
        await app.process_update(update)

        output = log_capture.getvalue()
        log_lines = []
        for line in output.strip().splitlines():
            try:
                parsed = json.loads(line)
                if parsed.get("event") == "update":
                    log_lines.append(parsed)
            except json.JSONDecodeError:
                continue

        assert len(log_lines) >= 1, f"Expected update log, got: {output}"
        log = log_lines[-1]
        assert log["user_id"] == "tg:42"
        assert log["update_type"] == "command"
        assert log["command"] == "/start"
        assert "duration_ms" in log

    @pytest.mark.asyncio
    @patch("telegram.Bot.initialize", new_callable=AsyncMock)
    @patch("telegram.Bot.get_me", new_callable=AsyncMock)
    async def test_handler_error_logged(self, mock_get_me, mock_bot_init, log_capture) -> None:
        app = ApplicationBuilder().token("fake:token").build()
        await app.initialize()

        async def _boom(update, context):
            raise ValueError("test boom")

        from telegram.ext import TypeHandler

        app.add_handler(TypeHandler(Update, _boom))
        install_update_logging(app)

        update = _make_update(text="/start")
        # process_update should not raise — error handler catches it
        await app.process_update(update)

        output = log_capture.getvalue()
        error_logs = []
        for line in output.strip().splitlines():
            try:
                parsed = json.loads(line)
                if parsed.get("event") == "handler_error":
                    error_logs.append(parsed)
            except json.JSONDecodeError:
                continue

        assert len(error_logs) >= 1, f"Expected error log, got: {output}"
        log = error_logs[-1]
        assert log["exception_type"] == "ValueError"
        assert "test boom" in log["exception_message"]
