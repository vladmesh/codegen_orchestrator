"""Production Application dispatch, fake Telegram HTTP, real protected PO Redis."""

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.ext import Application
from telegram.request import BaseRequest

from shared.contracts.queues.po import POInputMessage, POResponse, to_flat_fields
from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE
from shared.redis import RedisStreamClient
from src import main, middleware
from src.config import get_settings

CANARY = "opaque-dispatch-secret-1441"


class TelegramTransport(BaseRequest):
    def __init__(self):
        self.sent = []
        self.changed = asyncio.Event()
        self.callback_entered = asyncio.Event()
        self.release_callback = asyncio.Event()
        self.release_callback.set()

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        action = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if action == "getMe":
            result = {"id": 123, "is_bot": True, "first_name": "fixture", "username": "fixture"}
        elif action in ("sendMessage", "editMessageText"):
            if action == "editMessageText":
                self.callback_entered.set()
                await self.release_callback.wait()
            self.sent.append(params)
            self.changed.set()
            result = {
                "message_id": len(self.sent),
                "date": 0,
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "text": params["text"],
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    async def wait_text(self, user_id, text):
        async with asyncio.timeout(3):
            while True:
                self.changed.clear()
                if any(int(p["chat_id"]) == user_id and text in p["text"] for p in self.sent):
                    return
                await self.changed.wait()


def update(app, number, user_id, text=None, callback=None):
    message = {
        "message_id": number,
        "date": 0,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "fixture"},
        "text": text or "menu",
    }
    if text and text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text)}]
    body = {"update_id": number, "message": message}
    if callback:
        body = {
            "update_id": number,
            "callback_query": {
                "id": str(number),
                "from": message["from"],
                "chat_instance": "fixture",
                "message": message,
                "data": callback,
            },
        }
    return Update.de_json(body, app.bot)


@pytest.fixture
async def dispatch(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MAX_CONCURRENT_UPDATES", "8")
    get_settings.cache_clear()
    client = RedisStreamClient(os.environ["REDIS_URL"], stream_maxlen=0)
    await client.connect()
    await client.redis.flushdb()
    monkeypatch.setattr(main, "_stream_client", client)
    monkeypatch.setattr(
        middleware, "_check_user_in_db", AsyncMock(return_value={"is_admin": False})
    )
    wire = TelegramTransport()
    builder = Application.builder
    monkeypatch.setattr(Application, "builder", lambda: builder().request(wire).updater(None))
    captured = []
    monkeypatch.setattr(Application, "run_polling", lambda app, **kw: captured.append(app))
    main.main()
    app = captured[0]
    await app.initialize()
    await app.start()
    reader = client.consume_typed(
        PO_INPUT_QUEUE,
        PO_CONSUMER_GROUP,
        "dispatch-fixture",
        POInputMessage,
        block_ms=5,
    )

    async def next_input():
        async with asyncio.timeout(2):
            async for entry in reader:
                if entry is not None:
                    return entry
        raise AssertionError("Application did not dispatch the next user's PO input")

    async def reply(entry, text):
        stream = f"po:response:{entry.value.request_id}"
        await client.publish_flat(
            stream,
            to_flat_fields(
                POResponse(
                    text=text,
                    telegram_chat_id=entry.value.telegram_chat_id,
                )
            ),
        )
        assert CANARY not in str(await client.redis.xrange(stream))
        await client.ack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, entry.message_id)

    try:
        yield app, wire, client, next_input, reply
    finally:
        wire.release_callback.set()
        # Release held replies even after a red concurrency assertion, so stop drains.
        monkeypatch.setattr(main, "PO_RESPONSE_TIMEOUT_S", 0.1)
        for _, fields in await client.redis.xrange(PO_INPUT_QUEUE):
            from shared.contracts.queues.po import unprotect_po_payload

            logical = unprotect_po_payload(PO_INPUT_QUEUE, fields)
            await client.publish_flat(
                f"po:response:{logical['request_id']}",
                to_flat_fields(
                    POResponse(text="fixture-cleanup", telegram_chat_id=logical["telegram_chat_id"])
                ),
            )
        await asyncio.wait_for(app.stop(), 4)
        await app.shutdown()
        await reader.aclose()
        await client.redis.flushdb()
        await client.close()
        get_settings.cache_clear()


async def test_second_user_completes_while_first_po_reply_is_held(dispatch, capsys, caplog):
    app, wire, client, next_input, reply = dispatch
    await app.update_queue.put(update(app, 1, 101, CANARY + "-a"))
    first = await next_input()
    await app.update_queue.put(update(app, 2, 102, CANARY + "-b"))
    second = await next_input()
    assert (first.value.telegram_chat_id, second.value.telegram_chat_id) == ("101", "102")
    assert first.value.request_id != second.value.request_id
    assert first.value.text == CANARY + "-a"
    assert second.value.text == CANARY + "-b"
    assert CANARY not in str(await client.redis.xrange(PO_INPUT_QUEUE))
    await reply(second, CANARY + "-reply-b")
    await wire.wait_text(102, CANARY + "-reply-b")
    assert not any(int(p["chat_id"]) == 101 for p in wire.sent)
    await reply(first, CANARY + "-reply-a")
    await wire.wait_text(101, CANARY + "-reply-a")
    await asyncio.wait_for(app.update_queue.join(), 3)
    assert not await client.redis.keys("po:response:*")
    assert (await client.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 0
    assert not any(t.get_coro().__name__ == "_keep_typing" for t in asyncio.all_tasks())
    captured = capsys.readouterr()
    assert CANARY not in captured.out + captured.err + caplog.text


async def test_admin_callback_command_and_input_remain_ordered_and_authorized(
    dispatch, monkeypatch
):
    from src import handlers

    app, wire, client, next_input, reply = dispatch
    monkeypatch.setattr(
        middleware,
        "_check_user_in_db",
        AsyncMock(
            side_effect=lambda uid: {"is_admin": uid == 201} if uid != 203 else None,
        ),
    )
    monkeypatch.setattr(middleware, "_upsert_user", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers, "_required_int_config", AsyncMock(return_value=100))
    mint = AsyncMock(return_value=[{"code": "fixture-promo"}])
    monkeypatch.setattr(handlers.api_client, "post_json", mint)
    wire.release_callback.clear()
    await app.update_queue.put(update(app, 10, 201, callback="admin:add_user"))
    await asyncio.wait_for(wire.callback_entered.wait(), 2)
    await app.update_queue.put(update(app, 11, 201, "/menu"))
    await app.update_queue.put(update(app, 12, 201, "3000000001"))
    await app.update_queue.put(update(app, 13, 202, "/menu"))
    await app.update_queue.put(update(app, 14, 203, "unregistered"))
    await app.update_queue.put(update(app, 15, 203, "/menu"))
    await app.update_queue.put(update(app, 16, 203, callback="admin:add_user"))
    await wire.wait_text(202, "Главное меню")
    assert not any(int(p["chat_id"]) == 201 for p in wire.sent)
    mint.assert_not_awaited()
    wire.release_callback.set()
    await asyncio.wait_for(app.update_queue.join(), 3)
    admin_texts = [p["text"] for p in wire.sent if int(p["chat_id"]) == 201]
    assert "Добавить пользователя" in admin_texts[0]
    assert "Главное меню" in admin_texts[1]
    assert "fixture-promo" in admin_texts[2]
    mint.assert_awaited_once()
    assert not app.user_data[201].get("awaiting_add_user")
    assert app.user_data[201]["user_is_admin"] is True
    assert app.user_data[202]["user_is_admin"] is False
    assert not app.user_data[203].get("user_is_admin", False)
    await app.update_queue.put(update(app, 17, 202, callback="admin:add_user"))
    await asyncio.wait_for(app.update_queue.join(), 3)
    assert not app.user_data[202].get("awaiting_add_user")
    assert any(int(p["chat_id"]) == 202 and "Доступ запрещён" in p["text"] for p in wire.sent)
    assert not await client.redis.exists(PO_INPUT_QUEUE)
    # Churn many users: native admission must not retain idle lock objects.
    for uid in range(400, 450):
        await app.update_queue.put(update(app, uid, uid, "/menu"))
    await asyncio.wait_for(app.update_queue.join(), 3)
    assert not app.update_processor._users
