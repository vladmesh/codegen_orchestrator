"""Accepted direct PO replies remain owed across deadlines and bot lifetimes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fakeredis.aioredis import FakeRedis
import pytest
import pytest_asyncio

from shared.contracts.queues.po import MESSAGE_BREAK, POResponse, to_flat_fields
from shared.redis import RedisStreamClient
from shared.redis.po import verify_po_storage
from src import main
from src.po_delivery import (
    PENDING_KEY,
    POResponseListener,
    deliver_response,
    register_request,
)
from src.texts import text


@pytest_asyncio.fixture
async def client():
    client = RedisStreamClient(redis_url="redis://fake:6379")
    client._redis = FakeRedis(decode_responses=True)
    yield client
    await client.close()


async def _response(client, request_id, chat_id, answer, error=None):
    await client.publish_flat(
        f"po:response:{request_id}",
        to_flat_fields(POResponse(text=answer, telegram_chat_id=str(chat_id), error=error)),
    )


@pytest.mark.asyncio
async def test_late_answer_and_other_user_are_delivered_once(client, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("src.po_delivery.time.time", lambda: clock[0])
    await register_request(client.redis, "first", 101, "en", 60)
    await register_request(client.redis, "second", 202, "ru", 60)
    bot = AsyncMock()
    listener = POResponseListener(client)
    clock[0] = 60.0
    await listener.poll_once(bot)
    assert bot.send_message.await_count == 0
    assert await client.redis.zcard(PENDING_KEY) == 2

    clock[0] = 90.0
    await _response(client, "second", 202, "second answer")
    await _response(client, "first", 101, "first answer")
    await listener.poll_once(bot)
    await listener.poll_once(bot)
    delivered = {
        (call.kwargs["chat_id"], call.kwargs["text"]) for call in bot.send_message.await_args_list
    }
    assert delivered == {(101, "first answer"), (202, "second answer")}
    assert bot.send_message.await_count == 2
    assert await client.redis.zcard(PENDING_KEY) == 0


@pytest.mark.asyncio
async def test_timely_delivery_and_replay_have_one_owner(client):
    await register_request(client.redis, "timely", 101, "en", 60)
    response = POResponse(text="ready", telegram_chat_id="101")
    await _response(client, "timely", 101, "ready")
    bot = AsyncMock()
    assert await deliver_response(client.redis, bot, "timely", response)
    await POResponseListener(client).poll_once(bot)
    assert not await deliver_response(client.redis, bot, "timely", response)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_configured_timeout_notice_keeps_eventual_reply_deliverable(client, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("src.po_delivery.time.time", lambda: clock[0])
    update = MagicMock()
    update.effective_user.id = update.effective_chat.id = 101
    update.effective_user.first_name = "User"
    update.effective_user.language_code = "ru"
    update.message.text = "question"
    update.message.reply_text = AsyncMock()
    context = MagicMock(user_data={}, bot=AsyncMock())
    with (
        patch.object(main, "_stream_client", client),
        patch.object(main, "_config_store") as config_store,
        patch.object(main.uuid, "uuid4", return_value="configured-late"),
        patch.object(main, "_read_po_response", new=AsyncMock(return_value=None)) as read,
    ):
        config_store.get.return_value = 15
        await main.handle_message(update, context)
    update.message.reply_text.assert_awaited_once_with(text("ru", "po_timeout"))
    assert read.await_args.args[-1] == 15
    assert await client.redis.zcard(PENDING_KEY) == 1
    clock[0] = 116.0
    await _response(client, "configured-late", 101, "eventual answer")
    listener = POResponseListener(client)
    await listener.poll_once(context.bot)
    await listener.poll_once(context.bot)
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.kwargs["text"] == "eventual answer"
    assert await client.redis.zcard(PENDING_KEY) == 0


@pytest.mark.asyncio
async def test_direct_reply_uses_ordered_text_chunks(client):
    await register_request(client.redis, "chunks", 101, "en", 60)
    bot = AsyncMock()
    response = POResponse(text=MESSAGE_BREAK.join(["first", "second"]), telegram_chat_id="101")
    assert await deliver_response(client.redis, bot, "chunks", response)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == ["first", "second"]


@pytest.mark.asyncio
async def test_cancelled_wait_and_new_listener_recover_reply(client, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("src.po_delivery.time.time", lambda: clock[0])
    entered = asyncio.Event()

    async def wait_for_reply(*_args):
        entered.set()
        await asyncio.Event().wait()

    with (
        patch("src.main.uuid.uuid4", return_value="cancelled"),
        patch("src.main._config_store") as config_store,
        patch("src.main._read_po_response", side_effect=wait_for_reply),
    ):
        config_store.get.return_value = 60
        waiter = asyncio.create_task(
            main._send_to_po_and_wait(client, 101, "input", AsyncMock(), 101)
        )
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
    assert await client.redis.zcard(PENDING_KEY) == 1
    clock[0] = 161.0
    await _response(client, "cancelled", 101, "after restart")
    bot = AsyncMock()
    await POResponseListener(client).poll_once(bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "after restart"


@pytest.mark.asyncio
async def test_recipient_mismatch_keeps_obligation(client):
    await register_request(client.redis, "wrong", 101, "en", 60)
    bot = AsyncMock()
    with pytest.raises(RuntimeError):
        await deliver_response(
            client.redis, bot, "wrong", POResponse(text="private", telegram_chat_id="202")
        )
    bot.send_message.assert_not_awaited()
    assert await client.redis.zcard(PENDING_KEY) == 1


@pytest.mark.asyncio
async def test_one_bad_reply_does_not_hold_another_user(client):
    await register_request(client.redis, "bad", 101, "en", 0)
    await register_request(client.redis, "good", 202, "en", 0)
    await client.redis.xadd("po:response:bad", {"po_encrypted_v1": "invalid"})
    await _response(client, "good", 202, "good answer")
    bot = AsyncMock()
    await POResponseListener(client).poll_once(bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["chat_id"] == 202
    assert await client.redis.zcard(PENDING_KEY) == 1


@pytest.mark.asyncio
async def test_error_and_empty_reply_send_fixed_localized_text(client):
    bot = AsyncMock()
    for request_id, response in (
        ("error", POResponse(text="sensitive error", telegram_chat_id="101", error="true")),
        ("empty", POResponse(text="", telegram_chat_id="101")),
    ):
        await register_request(client.redis, request_id, 101, "ru", 60)
        await deliver_response(client.redis, bot, request_id, response)
    assert [call.kwargs["text"] for call in bot.send_message.await_args_list] == [
        text("ru", "message_failed"),
        text("ru", "message_failed"),
    ]


@pytest.mark.asyncio
async def test_released_pending_response_stream_is_recovered(client):
    await _response(client, "released", 101, "existing answer")
    bot = AsyncMock()
    await POResponseListener(client).poll_once(bot)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.kwargs["text"] == "existing answer"


@pytest.mark.asyncio
async def test_recipient_binding_is_authenticated_at_startup(client):
    await register_request(client.redis, "protected", 101, "en", 60)
    raw = await client.redis.hgetall("po:delivery:address:protected")
    assert "101" not in str(raw)
    await verify_po_storage(client.redis)
    await client.redis.hset("po:delivery:address:protected", mapping={"telegram_chat_id": "101"})
    with pytest.raises(RuntimeError, match="invalid_payload_keys"):
        await verify_po_storage(client.redis)
