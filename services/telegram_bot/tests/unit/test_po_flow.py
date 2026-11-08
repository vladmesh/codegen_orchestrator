"""Tests for direct PO ReactAgent communication via Redis Streams."""

import asyncio
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.config_store import ConfigStoreUnavailableError
from shared.contracts.queues.po import POResponse, protect_po_payload
from src.main import (
    _keep_typing,
    _read_po_response,
    _send_to_po_and_wait,
    handle_message,
)
from src.po_delivery import POResponsePending
from src.texts import text as bot_text


@pytest.fixture(autouse=True)
def configured_timeout(monkeypatch):
    store = MagicMock()
    store.get.return_value = 60
    monkeypatch.setattr("src.main._config_store", store, raising=False)
    return store


def response_xread(data):
    """The released protected wire, authenticated for the requested response stream."""

    def read(streams, **kwargs):
        stream = next(iter(streams))
        return [(stream, [("1-0", protect_po_payload(stream, data))])]

    return AsyncMock(side_effect=read)


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
        response_data = {"text": "Hello!", "telegram_chat_id": "123"}
        mock_redis.xread = response_xread(response_data)

        result = await _read_po_response(mock_redis, "po:response:abc", timeout_s=5.0)

        assert result == POResponse(**response_data)

    @pytest.mark.asyncio
    async def test_reads_from_id_zero(self):
        """Should read from id='0' to catch responses written before XREAD starts."""
        mock_redis = AsyncMock()
        mock_redis.xread = response_xread({"text": "ok", "telegram_chat_id": "123"})

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
    async def test_retries_on_transient_error(self, monkeypatch):
        """Should retry on transient Redis errors."""
        monkeypatch.setattr("src.main.PO_RESPONSE_READ_RETRY_DELAY_S", 0)
        mock_redis = AsyncMock()
        response_data = {"text": "recovered", "telegram_chat_id": "123"}
        mock_redis.xread = AsyncMock(
            side_effect=[
                ConnectionError("Redis gone"),
                [
                    (
                        "po:response:abc",
                        [("1-0", protect_po_payload("po:response:abc", response_data))],
                    )
                ],
            ]
        )

        result = await _read_po_response(mock_redis, "po:response:abc", timeout_s=5.0)

        assert result == POResponse(**response_data)
        assert mock_redis.xread.call_count == 2  # noqa: PLR2004


class TestSendToPOAndWait:
    """Tests for _send_to_po_and_wait."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seconds", [15, 120])
    async def test_configured_deadline_is_captured_for_both_consumers(
        self, configured_timeout, mock_stream_client, mock_bot, seconds
    ):
        configured_timeout.get.return_value = seconds
        with (
            patch("src.main.register_request", new_callable=AsyncMock) as register,
            patch("src.main._read_po_response", new_callable=AsyncMock) as read,
        ):
            read.return_value = POResponse(text="ready", telegram_chat_id="42")
            await _send_to_po_and_wait(mock_stream_client, 42, "hello", mock_bot, 42)
        assert register.await_args.args[-1] == seconds
        assert read.await_args.args[-1] == seconds
        configured_timeout.get.assert_called_once_with("telegram.po_response_timeout_seconds")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [None, 0, -2, math.nan, math.inf, "bad"])
    async def test_invalid_policy_refuses_before_registration(
        self, configured_timeout, mock_stream_client, mock_bot, value
    ):
        configured_timeout.get.return_value = value
        with pytest.raises((RuntimeError, ValueError)):
            await _send_to_po_and_wait(mock_stream_client, 42, "hello", mock_bot, 42)
        mock_stream_client.redis.hset.assert_not_awaited()
        mock_stream_client.publish_flat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_policy_refuses_before_registration(
        self, configured_timeout, mock_stream_client, mock_bot
    ):
        configured_timeout.get.side_effect = KeyError("telegram.po_response_timeout_seconds")
        with pytest.raises((RuntimeError, KeyError)):
            await _send_to_po_and_wait(mock_stream_client, 42, "hello", mock_bot, 42)
        mock_stream_client.redis.hset.assert_not_awaited()
        mock_stream_client.publish_flat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_policy_refuses_before_registration(
        self, configured_timeout, mock_stream_client, mock_bot
    ):
        configured_timeout.get.side_effect = ConfigStoreUnavailableError(
            "system config unavailable"
        )
        with pytest.raises(ConfigStoreUnavailableError):
            await _send_to_po_and_wait(mock_stream_client, 42, "hello", mock_bot, 42)
        mock_stream_client.redis.hset.assert_not_awaited()
        mock_stream_client.publish_flat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_successful_response(self, mock_stream_client, mock_bot):
        """Should return response text on success."""
        response_data = {"text": "Project created!", "telegram_chat_id": "42"}
        mock_stream_client.redis.xread = response_xread(response_data)

        with patch("src.main.uuid") as mock_uuid:
            mock_uuid.uuid4.return_value.hex = "a" * 32
            mock_uuid.uuid4.return_value.__str__ = lambda self: "test-request-id"

            result = await _send_to_po_and_wait(
                client=mock_stream_client,
                telegram_chat_id=42,
                text="Create a blog",
                bot=mock_bot,
                chat_id=42,
            )

        assert result[1] == POResponse(**response_data)

    @pytest.mark.asyncio
    async def test_message_format_plain_fields(self, mock_stream_client, mock_bot):
        """Pass logical flat fields to the client's protection boundary."""
        response_data = {"text": "ok", "telegram_chat_id": "42"}
        mock_stream_client.redis.xread = response_xread(response_data)

        await _send_to_po_and_wait(
            client=mock_stream_client,
            telegram_chat_id=42,
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
        assert fields["telegram_chat_id"] == "42"
        assert "request_id" in fields
        assert "timestamp" in fields

    @pytest.mark.asyncio
    async def test_message_includes_user_name(self, mock_stream_client, mock_bot):
        """Should include user_name in published fields."""
        response_data = {"text": "ok", "telegram_chat_id": "42"}
        mock_stream_client.redis.xread = response_xread(response_data)

        await _send_to_po_and_wait(
            client=mock_stream_client,
            telegram_chat_id=42,
            text="hello",
            bot=mock_bot,
            chat_id=42,
            user_name="Vlad",
        )

        publish_call = mock_stream_client.publish_flat.call_args
        fields = publish_call[0][1]
        assert fields["user_name"] == "Vlad"

    @pytest.mark.asyncio
    async def test_error_response_remains_owed(self, mock_stream_client, mock_bot):
        """The error response stays owed until a safe apology is delivered."""
        error_data = {
            "text": "An error occurred, please try again.",
            "telegram_chat_id": "42",
            "error": "true",
        }
        mock_stream_client.redis.xread = response_xread(error_data)

        _, response = await _send_to_po_and_wait(
            client=mock_stream_client,
            telegram_chat_id=42,
            text="hello",
            bot=mock_bot,
            chat_id=42,
        )
        assert response.error == "true"

    @pytest.mark.asyncio
    async def test_timeout_raises_timeout_error(self, mock_stream_client, mock_bot):
        """A deadline ends typing without discarding the accepted reply."""
        with (
            patch("src.main._read_po_response", new=AsyncMock(return_value=None)),
            pytest.raises(TimeoutError),
        ):
            await _send_to_po_and_wait(
                client=mock_stream_client,
                telegram_chat_id=42,
                text="hello",
                bot=mock_bot,
                chat_id=42,
            )
        mock_stream_client.redis.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_notice_is_localized(self, mock_stream_client):
        """A timed-out accepted request gets a pending notice in its chat language."""
        update = MagicMock()
        update.effective_user.id = update.effective_chat.id = 42
        update.effective_user.first_name = "User"
        update.effective_user.language_code = "ru"
        update.message.text = "request"
        update.message.reply_text = AsyncMock()
        context = MagicMock(user_data={}, bot=AsyncMock())
        with (
            patch("src.main._stream_client", mock_stream_client),
            patch(
                "src.main._send_to_po_and_wait",
                new=AsyncMock(side_effect=POResponsePending("r")),
            ),
            patch("src.main.is_settled", new=AsyncMock(return_value=False)),
        ):
            await handle_message(update, context)
        update.message.reply_text.assert_awaited_once_with(bot_text("ru", "po_timeout"))

    @pytest.mark.asyncio
    async def test_settled_reply_does_not_get_later_pending_notice(self, mock_stream_client):
        """The listener may settle the reply at the interactive deadline."""
        update = MagicMock()
        update.effective_user.id = update.effective_chat.id = 42
        update.effective_user.first_name = "User"
        update.effective_user.language_code = "en"
        update.message.text = "request"
        update.message.reply_text = AsyncMock()
        context = MagicMock(user_data={}, bot=AsyncMock())
        with (
            patch("src.main._stream_client", mock_stream_client),
            patch(
                "src.main._send_to_po_and_wait",
                new=AsyncMock(side_effect=POResponsePending("r")),
            ),
            patch("src.main.is_settled", new=AsyncMock(return_value=True)),
        ):
            await handle_message(update, context)
        update.message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_response_remains_owed_until_delivery(self, mock_stream_client, mock_bot):
        """Reading a reply alone cannot settle delivery."""
        response_data = {"text": "done", "telegram_chat_id": "42"}
        mock_stream_client.redis.xread = response_xread(response_data)

        await _send_to_po_and_wait(
            client=mock_stream_client,
            telegram_chat_id=42,
            text="hello",
            bot=mock_bot,
            chat_id=42,
        )

        mock_stream_client.redis.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_error_response_remains_owed_until_delivery(self, mock_stream_client, mock_bot):
        """An error reply also has a delivery obligation."""
        error_data = {"text": "error", "telegram_chat_id": "42", "error": "true"}
        mock_stream_client.redis.xread = response_xread(error_data)

        await _send_to_po_and_wait(
            client=mock_stream_client,
            telegram_chat_id=42,
            text="hello",
            bot=mock_bot,
            chat_id=42,
        )
        mock_stream_client.redis.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_response_remains_owed(self, mock_stream_client, mock_bot):
        """An empty reply is settled through the safe apology path."""
        empty_data = {"text": "", "telegram_chat_id": "42"}
        mock_stream_client.redis.xread = response_xread(empty_data)

        _, response = await _send_to_po_and_wait(
            client=mock_stream_client,
            telegram_chat_id=42,
            text="hello",
            bot=mock_bot,
            chat_id=42,
        )
        assert response.text == ""

    @pytest.mark.asyncio
    async def test_typing_indicator_started_and_cancelled(self, mock_stream_client, mock_bot):
        """Should start typing task and cancel it after response."""
        response_data = {"text": "done", "telegram_chat_id": "42"}

        # Delay xread response so typing task has time to fire
        async def delayed_xread(*args, **kwargs):
            await asyncio.sleep(0)  # yield control to let typing task run
            stream = next(iter(args[0]))
            return [(stream, [("1-0", protect_po_payload(stream, response_data))])]

        mock_stream_client.redis.xread = AsyncMock(side_effect=delayed_xread)

        with patch("src.main.TYPING_INTERVAL_S", 0.01):
            await _send_to_po_and_wait(
                client=mock_stream_client,
                telegram_chat_id=42,
                text="hello",
                bot=mock_bot,
                chat_id=42,
            )

        # Typing should have fired at least once before response arrived
        assert mock_bot.send_chat_action.call_count >= 1
