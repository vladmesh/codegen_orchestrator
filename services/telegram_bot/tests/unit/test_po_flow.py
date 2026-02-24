"""Tests for direct PO ReactAgent communication via Redis Streams."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.main import (
    _keep_typing,
    _read_po_response,
    _send_to_po_and_wait,
)


@pytest.fixture
def mock_stream_client():
    """Create a mock RedisStreamClient."""
    client = AsyncMock()
    client.redis = AsyncMock()
    client.redis.xread = AsyncMock(return_value=[])
    client.redis.delete = AsyncMock()
    client.publish_flat = AsyncMock()
    return client


@pytest.fixture
def mock_bot():
    """Create a mock Telegram bot."""
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()
    return bot


class TestKeepTyping:
    """Tests for _keep_typing."""

    @pytest.mark.asyncio
    async def test_sends_typing_action(self, mock_bot):
        """Should send typing action at least once."""
        task = asyncio.create_task(_keep_typing(mock_bot, chat_id=123, max_duration_s=0.1))
        await asyncio.sleep(0)  # yield control so task runs
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_bot.send_chat_action.assert_called_with(chat_id=123, action="typing")

    @pytest.mark.asyncio
    async def test_safely_cancellable(self, mock_bot):
        """Should handle cancellation gracefully."""
        task = asyncio.create_task(_keep_typing(mock_bot, chat_id=123))
        await asyncio.sleep(0)  # yield control so task starts
        task.cancel()

        # Should not raise
        await task

    @pytest.mark.asyncio
    async def test_respects_max_duration(self, mock_bot):
        """Should stop after max_duration_s."""
        # With 0 max_duration, should exit immediately
        await _keep_typing(mock_bot, chat_id=123, max_duration_s=0)
        # No assertion needed - just verify it doesn't hang


class TestReadPOResponse:
    """Tests for _read_po_response."""

    @pytest.mark.asyncio
    async def test_returns_data_on_response(self):
        """Should return response data when available."""
        mock_redis = AsyncMock()
        response_data = {"text": "Hello!", "user_id": "123"}
        mock_redis.xread = AsyncMock(return_value=[("po:response:abc", [("1-0", response_data)])])

        result = await _read_po_response(mock_redis, "po:response:abc", timeout_s=5.0)

        assert result == response_data

    @pytest.mark.asyncio
    async def test_reads_from_id_zero(self):
        """Should read from id='0' to catch responses written before XREAD starts."""
        mock_redis = AsyncMock()
        mock_redis.xread = AsyncMock(return_value=[("po:response:abc", [("1-0", {"text": "ok"})])])

        await _read_po_response(mock_redis, "po:response:abc", timeout_s=5.0)

        call_args = mock_redis.xread.call_args
        streams_arg = call_args.kwargs.get("streams") or call_args.args[0]
        assert streams_arg == {"po:response:abc": "0"}

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self):
        """Should return None when timeout expires."""
        mock_redis = AsyncMock()
        mock_redis.xread = AsyncMock(return_value=[])

        result = await _read_po_response(mock_redis, "po:response:abc", timeout_s=0.1)

        assert result is None

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        """Should retry on transient Redis errors."""
        mock_redis = AsyncMock()
        response_data = {"text": "recovered", "user_id": "123"}
        mock_redis.xread = AsyncMock(
            side_effect=[
                ConnectionError("Redis gone"),
                [("po:response:abc", [("1-0", response_data)])],
            ]
        )

        result = await _read_po_response(mock_redis, "po:response:abc", timeout_s=5.0)

        assert result == response_data
        assert mock_redis.xread.call_count == 2  # noqa: PLR2004


class TestSendToPOAndWait:
    """Tests for _send_to_po_and_wait."""

    @pytest.mark.asyncio
    async def test_successful_response(self, mock_stream_client, mock_bot):
        """Should return response text on success."""
        response_data = {"text": "Project created!", "user_id": "42"}
        mock_stream_client.redis.xread = AsyncMock(
            return_value=[("po:response:test-id", [("1-0", response_data)])]
        )

        with patch("src.main.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value.hex = "a" * 32
            mock_uuid.uuid4.return_value.__str__ = lambda self: "test-request-id"

            result = await _send_to_po_and_wait(
                client=mock_stream_client,
                user_id=42,
                text="Create a blog",
                bot=mock_bot,
                chat_id=42,
            )

        assert result == "Project created!"

    @pytest.mark.asyncio
    async def test_message_format_plain_fields(self, mock_stream_client, mock_bot):
        """Should send plain fields to po:input (not JSON-wrapped)."""
        response_data = {"text": "ok", "user_id": "42"}
        mock_stream_client.redis.xread = AsyncMock(
            return_value=[("po:response:test-id", [("1-0", response_data)])]
        )

        await _send_to_po_and_wait(
            client=mock_stream_client,
            user_id=42,
            text="hello",
            bot=mock_bot,
            chat_id=42,
        )

        publish_call = mock_stream_client.publish_flat.call_args
        stream_name = publish_call[0][0]
        fields = publish_call[0][1]

        assert stream_name == "po:input"
        assert fields["type"] == "user_message"
        assert fields["text"] == "hello"
        assert fields["user_id"] == "42"
        assert "request_id" in fields
        assert "timestamp" in fields

    @pytest.mark.asyncio
    async def test_error_response_raises_runtime_error(self, mock_stream_client, mock_bot):
        """Should raise RuntimeError when PO returns error."""
        error_data = {
            "text": "An error occurred, please try again.",
            "user_id": "42",
            "error": "true",
        }
        mock_stream_client.redis.xread = AsyncMock(
            return_value=[("po:response:test-id", [("1-0", error_data)])]
        )

        with pytest.raises(RuntimeError, match="An error occurred"):
            await _send_to_po_and_wait(
                client=mock_stream_client,
                user_id=42,
                text="hello",
                bot=mock_bot,
                chat_id=42,
            )

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self, mock_stream_client, mock_bot):
        """Should raise TimeoutError when PO doesn't respond in time."""
        mock_stream_client.redis.xread = AsyncMock(return_value=[])

        with (
            patch("src.main.PO_RESPONSE_TIMEOUT_S", 0.1),
            pytest.raises(TimeoutError),
        ):
            await _send_to_po_and_wait(
                client=mock_stream_client,
                user_id=42,
                text="hello",
                bot=mock_bot,
                chat_id=42,
            )

    @pytest.mark.asyncio
    async def test_stream_cleanup_after_success(self, mock_stream_client, mock_bot):
        """Should delete response stream after reading."""
        response_data = {"text": "done", "user_id": "42"}
        mock_stream_client.redis.xread = AsyncMock(
            return_value=[("po:response:test-id", [("1-0", response_data)])]
        )

        await _send_to_po_and_wait(
            client=mock_stream_client,
            user_id=42,
            text="hello",
            bot=mock_bot,
            chat_id=42,
        )

        # Verify delete was called with the response stream
        mock_stream_client.redis.delete.assert_called_once()
        deleted_stream = mock_stream_client.redis.delete.call_args.args[0]
        assert deleted_stream.startswith("po:response:")

    @pytest.mark.asyncio
    async def test_stream_cleanup_after_error(self, mock_stream_client, mock_bot):
        """Should delete response stream even after error."""
        error_data = {"text": "error", "user_id": "42", "error": "true"}
        mock_stream_client.redis.xread = AsyncMock(
            return_value=[("po:response:test-id", [("1-0", error_data)])]
        )

        with pytest.raises(RuntimeError):
            await _send_to_po_and_wait(
                client=mock_stream_client,
                user_id=42,
                text="hello",
                bot=mock_bot,
                chat_id=42,
            )

        mock_stream_client.redis.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_response_raises(self, mock_stream_client, mock_bot):
        """Should raise RuntimeError when PO returns empty text."""
        empty_data = {"text": "", "user_id": "42"}
        mock_stream_client.redis.xread = AsyncMock(
            return_value=[("po:response:test-id", [("1-0", empty_data)])]
        )

        with pytest.raises(RuntimeError, match="empty response"):
            await _send_to_po_and_wait(
                client=mock_stream_client,
                user_id=42,
                text="hello",
                bot=mock_bot,
                chat_id=42,
            )

    @pytest.mark.asyncio
    async def test_typing_indicator_started_and_cancelled(self, mock_stream_client, mock_bot):
        """Should start typing task and cancel it after response."""
        response_data = {"text": "done", "user_id": "42"}

        # Delay xread response so typing task has time to fire
        async def delayed_xread(*args, **kwargs):
            await asyncio.sleep(0)  # yield control to let typing task run
            return [("po:response:test-id", [("1-0", response_data)])]

        mock_stream_client.redis.xread = AsyncMock(side_effect=delayed_xread)

        with patch("src.main.TYPING_INTERVAL_S", 0.01):
            await _send_to_po_and_wait(
                client=mock_stream_client,
                user_id=42,
                text="hello",
                bot=mock_bot,
                chat_id=42,
            )

        # Typing should have fired at least once before response arrived
        assert mock_bot.send_chat_action.call_count >= 1
