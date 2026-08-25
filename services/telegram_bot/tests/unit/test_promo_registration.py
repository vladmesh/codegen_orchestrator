"""Unregistered Telegram users must not enter the PO flow."""

from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from src import middleware


@pytest.mark.asyncio
async def test_unregistered_message_is_not_sent_to_po(monkeypatch) -> None:
    update = AsyncMock()
    update.effective_user.id = 42
    update.effective_user.first_name = "New"
    update.effective_chat.id = 42
    update.message.text = "hello"
    update.message.reply_text = AsyncMock()
    context = AsyncMock()
    context.user_data = {}

    monkeypatch.setattr(middleware, "_check_user_in_db", AsyncMock(return_value=None))
    monkeypatch.setattr(middleware, "_upsert_user", AsyncMock(return_value=False))

    with pytest.raises(ApplicationHandlerStop):
        await middleware.auth_middleware(update, context)

    update.message.reply_text.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "command", "callback"])
async def test_unknown_update_is_stopped_for_every_update_kind(monkeypatch, kind: str) -> None:
    update = AsyncMock()
    update.effective_user.id = 43
    update.effective_user.username = "unknown"
    update.message = AsyncMock() if kind != "callback" else None
    if update.message is not None:
        update.message.text = "/start" if kind == "command" else "hello"
    if kind == "callback":
        update.callback_query = AsyncMock()
    context = AsyncMock()
    context.user_data = {}
    monkeypatch.setattr(middleware, "_check_user_in_db", AsyncMock(return_value=None))
    monkeypatch.setattr(middleware, "_upsert_user", AsyncMock(return_value=False))

    with pytest.raises(ApplicationHandlerStop):
        await middleware.auth_middleware(update, context)
