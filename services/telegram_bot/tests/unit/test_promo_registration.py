"""Unregistered Telegram users must not enter the PO flow."""

from unittest.mock import AsyncMock

import pytest

from src import main


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

    monkeypatch.setattr(main, "auth_middleware", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "_ensure_user_registered", AsyncMock(return_value=False))
    send_to_po = AsyncMock()
    monkeypatch.setattr(main, "_send_to_po_and_wait", send_to_po)

    await main.handle_message(update, context)

    send_to_po.assert_not_awaited()
    update.message.reply_text.assert_awaited()
