"""Rendered Telegram request and handler diagnostics must not expose bot tokens."""

from __future__ import annotations

from datetime import UTC, datetime
import importlib
import io
import json
import logging
import sys

import httpx
import pytest
import structlog
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationBuilder, TypeHandler
from telegram.request import HTTPXRequest

from services.tg_bot.src.middleware import install_update_logging
from shared.logging import configure_logging

TOKEN = "123456789:synthetic_canary_token_abcdefghijklmnopqrstuvwxyz"  # noqa: S105
TELEGRAM_ID = 8202532144


def _respond(request: httpx.Request) -> httpx.Response:
    method = request.url.path.rsplit("/", 1)[-1]
    if method == "sendMessage" and b"fail-canary" in request.content:
        raise httpx.ReadError(f"synthetic transport failure: {request.url}", request=request)
    result = (
        {"id": 123456789, "is_bot": True, "first_name": "Canary", "username": "canary_bot"}
        if method == "getMe"
        else True
    )
    if method == "sendMessage":
        result = {
            "message_id": 1,
            "date": 0,
            "chat": {"id": TELEGRAM_ID, "type": "private"},
            "text": "canary",
        }
    return httpx.Response(200, json={"ok": True, "result": result})


class LocalTelegramRequest(HTTPXRequest):
    """Exercise python-telegram-bot/httpx with responses confined to this process."""

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(_respond))


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
@pytest.mark.parametrize("console", [False, True])
async def test_startup_request_and_handler_logs_hide_token(
    monkeypatch: pytest.MonkeyPatch, level: str, console: bool
) -> None:
    output = io.StringIO()
    monkeypatch.setattr(output, "isatty", lambda: console)
    monkeypatch.setattr(sys, "stdout", output)
    root = logging.getLogger()
    handlers, root_level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        # Import the real entrypoint, whose startup configures shared logging.
        from services.tg_bot.src import main

        importlib.reload(main)
        if level == "DEBUG":
            configure_logging("tg_bot", log_level=level)
        app = (
            ApplicationBuilder()
            .token(TOKEN)
            .request(LocalTelegramRequest())
            .get_updates_request(LocalTelegramRequest())
            .build()
        )
        install_update_logging(app)

        async def send(update: Update, context: object) -> None:
            await app.bot.send_message(chat_id=TELEGRAM_ID, text="fail-canary")

        app.add_handler(TypeHandler(Update, send))
        await app.initialize()
        try:
            assert await app.bot.delete_webhook()
            sent = await app.bot.send_message(chat_id=TELEGRAM_ID, text="canary")
            assert sent.chat.id == TELEGRAM_ID
            update = Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=datetime.fromtimestamp(0, UTC),
                    chat=Chat(TELEGRAM_ID, "private"),
                    from_user=User(TELEGRAM_ID, "Canary", False),
                    text="canary",
                ),
            )
            await app.process_update(update)
            structlog.get_logger().info("safe_service_diagnostic", method="deleteWebhook")
        finally:
            await app.shutdown()
        rendered = output.getvalue()
        assert TOKEN not in rendered
        assert "handler_error" in rendered
        assert "NetworkError" in rendered
        assert "synthetic transport failure" in rendered
        assert "sendMessage" in rendered
        assert "safe_service_diagnostic" in rendered
        assert "tg_bot" in rendered
        if not console:
            errors = [
                json.loads(line)
                for line in rendered.splitlines()
                if json.loads(line).get("event") == "handler_error"
            ]
            assert len(errors) == 1
            assert "Traceback" in errors[0]["exception"]
            assert "synthetic transport failure" in errors[0]["exception_message"]
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        for name, previous in levels.items():
            logging.getLogger(name).setLevel(previous)
        structlog.contextvars.clear_contextvars()
