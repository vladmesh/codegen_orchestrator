"""Harmless Telegram fixtures exercise the real PO Redis transport entry points."""

import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock

from cryptography.fernet import Fernet
import pytest
import structlog

from shared.contracts.queues.po import (
    POInputMessage,
    POPayloadProtectionError,
    POProactiveMessage,
    POResponse,
    to_flat_fields,
)
from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE, PO_PROACTIVE_GROUP, PO_PROACTIVE_QUEUE
from shared.redis import RedisStreamClient
from src.main import _read_po_response, _send_to_po_and_wait
from src.proactive import ProactiveOutcome, process_proactive_entry

CANARIES = (
    "1234567890:AAredis_test_token_abcdefgh",
    "sk-or-v1-redis_test_provider_abcdefgh",
    "GOCSPX-redis_test_oauth_abcdefgh",
    "wqzj mvnp frtk bxhs",
)
TEXT = " / ".join(CANARIES)
KEY = "wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc="


def no_secrets(value):
    for canary in CANARIES:
        assert canary not in str(value)


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", KEY)
    client = RedisStreamClient(os.environ["REDIS_URL"], stream_maxlen=0)
    await client.connect()
    await client.redis.flushdb()
    yield client
    await client.redis.flushdb()
    await client.close()


@pytest.fixture(autouse=True)
def logs(caplog, capsys, monkeypatch):
    from shared.redis import client as redis_client
    from src import main, proactive

    previous = structlog.get_config()
    events = []

    def collect(logger, method_name, event_dict):
        events.append(event_dict.copy())
        return event_dict

    structlog.configure(
        processors=[
            collect,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    for module in (main, proactive, redis_client):
        monkeypatch.setattr(module, "logger", structlog.get_logger(module.__name__))
    caplog.set_level(logging.DEBUG)
    yield
    structlog.configure(**previous)
    captured = capsys.readouterr()
    assert events, "the log capture must exercise emitted diagnostics"
    no_secrets(str(events) + caplog.text + captured.out + captured.err)


async def test_actual_telegram_input_and_direct_response_roundtrip(client, monkeypatch):
    monkeypatch.setattr("src.main.uuid.uuid4", lambda: "canary-request")
    bot = AsyncMock()
    send = asyncio.create_task(
        _send_to_po_and_wait(client, 14310001, TEXT, bot, 14310001, user_name=TEXT)
    )
    reader = client.consume_typed(
        PO_INPUT_QUEUE, PO_CONSUMER_GROUP, "fixture-po", POInputMessage, block_ms=5
    )
    try:
        async with asyncio.timeout(5):
            async for entry in reader:
                if entry is not None:
                    break
            no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))
            assert entry.value.text == entry.value.user_name == TEXT
            response_stream = f"po:response:{entry.value.request_id}"
            await client.publish_flat(
                response_stream, to_flat_fields(POResponse(text=TEXT, telegram_chat_id="14310001"))
            )
            no_secrets(await client.redis.xrange(response_stream))
            await client.ack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, entry.message_id)
            assert await send == TEXT
            no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))
            assert not await client.redis.exists(response_stream)
    finally:
        await reader.aclose()
        if not send.done():
            send.cancel()
        await asyncio.gather(send, return_exceptions=True)


async def test_reply_delivery_exception_does_not_render_echoed_secret(client, monkeypatch):
    from src import main

    monkeypatch.setattr(main, "_stream_client", client)
    monkeypatch.setattr(main, "_send_response_to_user", AsyncMock(side_effect=ValueError(TEXT)))
    update = MagicMock()
    update.effective_user.id = update.effective_chat.id = 14310001
    update.effective_user.first_name = TEXT
    update.message.text = TEXT
    update.message.reply_text = AsyncMock()
    context = MagicMock(user_data={}, bot=AsyncMock())
    handler = asyncio.create_task(main.handle_message(update, context))
    reader = client.consume_typed(
        PO_INPUT_QUEUE, PO_CONSUMER_GROUP, "fixture-po", POInputMessage, block_ms=5
    )
    try:
        async with asyncio.timeout(5):
            async for entry in reader:
                if entry is not None:
                    break
            await client.publish_flat(
                f"po:response:{entry.value.request_id}",
                to_flat_fields(POResponse(text=TEXT, telegram_chat_id="14310001")),
            )
            await client.ack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, entry.message_id)
            await handler
            update.message.reply_text.assert_awaited_once_with(main.MESSAGE_FAILED_REPLY)
            no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))
    finally:
        await reader.aclose()
        if not handler.done():
            handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)


async def proactive_entry(client):
    reader = client.consume(
        PO_PROACTIVE_QUEUE,
        PO_PROACTIVE_GROUP,
        "bot",
        block_ms=5,
        auto_ack=False,
        claim_pending=True,
        pending_timeout_ms=0,
    )
    try:
        async with asyncio.timeout(3):
            async for entry in reader:
                if entry is not None:
                    return entry
    finally:
        await reader.aclose()


async def test_proactive_parsing_delivery_reclaim_and_retention(client):
    message = POProactiveMessage(text=TEXT, telegram_chat_id="14310001")
    await client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(message))
    no_secrets(await client.redis.xrange(PO_PROACTIVE_QUEUE))
    first = await proactive_entry(client)
    reclaimed = await proactive_entry(client)
    assert reclaimed.reclaimed and reclaimed.message_id == first.message_id
    bot = AsyncMock()
    assert await process_proactive_entry(bot, client, reclaimed) == ProactiveOutcome.DELIVERED
    assert bot.send_message.call_args.kwargs["text"] == TEXT
    assert (await client.redis.xpending(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP))["pending"] == 0
    no_secrets(await client.redis.xrange(PO_PROACTIVE_QUEUE))


async def test_delivery_failure_and_validation_do_not_echo_secrets(client, monkeypatch):
    alert = AsyncMock()
    monkeypatch.setattr("src.proactive.notify_admins_best_effort", alert)
    monkeypatch.setattr("src.proactive.PROACTIVE_RETRY_DELAY_S", 0)
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError(TEXT)
    await client.publish_flat(
        PO_PROACTIVE_QUEUE,
        to_flat_fields(
            POProactiveMessage(
                text=TEXT,
                telegram_chat_id="14310001",
                event=TEXT,
                story_id=TEXT,
                project_id=TEXT,
            )
        ),
    )
    assert (
        await process_proactive_entry(bot, client, await proactive_entry(client))
        == ProactiveOutcome.EXHAUSTED
    )
    no_secrets(alert.call_args_list)
    await client.publish_flat(
        PO_PROACTIVE_QUEUE, {"text": TEXT, "telegram_chat_id": {"echo": TEXT}}
    )
    assert (
        await process_proactive_entry(bot, client, await proactive_entry(client))
        == ProactiveOutcome.REJECTED
    )
    await client.publish_flat(PO_PROACTIVE_QUEUE, {"text": TEXT, "telegram_chat_id": TEXT})
    assert (
        await process_proactive_entry(bot, client, await proactive_entry(client))
        == ProactiveOutcome.REJECTED
    )
    no_secrets(await client.redis.xrange(PO_PROACTIVE_QUEUE))


@pytest.mark.parametrize("bad", ["plaintext", "corrupt", "wrong_key", "missing_key", "invalid_key"])
async def test_direct_response_refuses_unprotected_or_unauthenticated_payload(
    client, monkeypatch, bad
):
    stream = "po:response:refused"
    if bad == "plaintext":
        await client.redis.xadd(stream, {"text": TEXT})
    elif bad == "corrupt":
        await client.redis.xadd(stream, {"po_encrypted_v1": TEXT})
    else:
        await client.publish_flat(
            stream, to_flat_fields(POResponse(text=TEXT, telegram_chat_id="14310001"))
        )
        if bad == "wrong_key":
            monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        elif bad == "missing_key":
            monkeypatch.delenv("SECRETS_ENCRYPTION_KEY")
        else:
            monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "invalid-key")
    with pytest.raises(POPayloadProtectionError) as failure:
        await _read_po_response(client.redis, stream, 1)
    no_secrets(str(failure.value))
