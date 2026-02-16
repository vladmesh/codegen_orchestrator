"""Tests for ProactiveListener."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.main import ProactiveListener


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock()
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xack = AsyncMock()
    return redis


@pytest.fixture
def mock_bot():
    """Create a mock Telegram bot."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


class TestProactiveListener:
    @pytest.mark.asyncio
    async def test_reads_proactive_stream(self, mock_redis, mock_bot):
        """Should read messages from po:proactive stream."""
        message_data = {"user_id": "42", "text": "Your project is ready!"}
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [("po:proactive", [("1-0", message_data)])],
                asyncio.CancelledError(),
            ]
        )

        listener = ProactiveListener(redis=mock_redis)
        task = await listener.start(mock_bot)

        # Wait a bit for the loop to process
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_sends_to_correct_user(self, mock_redis, mock_bot):
        """Should send message to the user_id from stream data."""
        message_data = {"user_id": "12345", "text": "Deploy done!"}
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [("po:proactive", [("1-0", message_data)])],
                asyncio.CancelledError(),
            ]
        )

        listener = ProactiveListener(redis=mock_redis)
        task = await listener.start(mock_bot)
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # First attempt is with Markdown
        call_args = mock_bot.send_message.call_args
        assert call_args.kwargs["chat_id"] == 12345  # noqa: PLR2004

    @pytest.mark.asyncio
    async def test_acks_after_send(self, mock_redis, mock_bot):
        """Should ACK the message after sending."""
        message_data = {"user_id": "42", "text": "Hello!"}
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [("po:proactive", [("msg-1", message_data)])],
                asyncio.CancelledError(),
            ]
        )

        listener = ProactiveListener(redis=mock_redis)
        task = await listener.start(mock_bot)
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_redis.xack.assert_called_once_with("po:proactive", "tg-bot-proactive", "msg-1")

    @pytest.mark.asyncio
    async def test_handles_send_error_gracefully(self, mock_redis, mock_bot):
        """Should continue processing even if send_message fails."""
        mock_bot.send_message = AsyncMock(side_effect=Exception("Telegram API error"))

        message_data = {"user_id": "42", "text": "Hello!"}
        mock_redis.xreadgroup = AsyncMock(
            side_effect=[
                [("po:proactive", [("msg-1", message_data)])],
                asyncio.CancelledError(),
            ]
        )

        listener = ProactiveListener(redis=mock_redis)
        task = await listener.start(mock_bot)
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should still ACK even on send failure
        mock_redis.xack.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancellation(self, mock_redis, mock_bot):
        """Should exit cleanly on cancellation."""
        mock_redis.xreadgroup = AsyncMock(side_effect=asyncio.CancelledError())

        listener = ProactiveListener(redis=mock_redis)
        task = await listener.start(mock_bot)

        # Should not raise
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_creates_consumer_group(self, mock_redis, mock_bot):
        """Should create consumer group on startup."""
        mock_redis.xreadgroup = AsyncMock(side_effect=asyncio.CancelledError())

        listener = ProactiveListener(redis=mock_redis)
        task = await listener.start(mock_bot)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_redis.xgroup_create.assert_called_once_with(
            "po:proactive", "tg-bot-proactive", id="0", mkstream=True
        )

    @pytest.mark.asyncio
    async def test_handles_busygroup(self, mock_redis, mock_bot):
        """Should handle BUSYGROUP (group already exists) gracefully."""
        mock_redis.xgroup_create = AsyncMock(
            side_effect=Exception("BUSYGROUP Consumer Group name already exists")
        )
        mock_redis.xreadgroup = AsyncMock(side_effect=asyncio.CancelledError())

        listener = ProactiveListener(redis=mock_redis)
        task = await listener.start(mock_bot)
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Should not raise, just continue
