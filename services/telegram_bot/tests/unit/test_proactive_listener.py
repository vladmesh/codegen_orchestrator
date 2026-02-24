"""Tests for ProactiveListener."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.redis.client import StreamMessage
from src.main import ProactiveListener


@pytest.fixture
def mock_client():
    """Create a mock RedisStreamClient."""
    client = MagicMock()
    client.consume = MagicMock()
    client.ack = AsyncMock()
    return client


@pytest.fixture
def mock_bot():
    """Create a mock Telegram bot."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


def _make_consume_iter(messages):
    """Create an async iterator that yields messages then None then raises CancelledError."""

    async def _consume(*args, **kwargs):
        for msg in messages:
            yield msg
        yield None  # idle signal
        raise asyncio.CancelledError()

    return _consume


class TestProactiveListener:
    @pytest.mark.asyncio
    async def test_reads_proactive_stream(self, mock_client, mock_bot):
        """Should read messages from po:proactive stream."""
        msg = StreamMessage(
            message_id="1-0", data={"user_id": "42", "text": "Your project is ready!"}
        )
        mock_client.consume = _make_consume_iter([msg])

        listener = ProactiveListener(client=mock_client)
        task = await listener.start(mock_bot)

        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_to_correct_user(self, mock_client, mock_bot):
        """Should send message to the user_id from stream data."""
        msg = StreamMessage(message_id="1-0", data={"user_id": "12345", "text": "Deploy done!"})
        mock_client.consume = _make_consume_iter([msg])

        listener = ProactiveListener(client=mock_client)
        task = await listener.start(mock_bot)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # First attempt is with Markdown
        call_args = mock_bot.send_message.call_args
        assert call_args.kwargs["chat_id"] == 12345  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_handles_send_error_gracefully(self, mock_client, mock_bot):
        """Should continue processing even if send_message fails."""
        mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram API error"))

        msg = StreamMessage(message_id="msg-1", data={"user_id": "42", "text": "Hello!"})
        mock_client.consume = _make_consume_iter([msg])

        listener = ProactiveListener(client=mock_client)
        task = await listener.start(mock_bot)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should not crash — auto_ack handles ACK

    @pytest.mark.asyncio
    async def test_cancellation(self, mock_client, mock_bot):
        """Should exit cleanly on cancellation."""
        mock_client.consume = _make_consume_iter([])

        listener = ProactiveListener(client=mock_client)
        task = await listener.start(mock_bot)

        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
