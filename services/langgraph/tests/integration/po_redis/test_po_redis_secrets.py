"""Read complete PO payloads back from real Redis, including retained ACKed work."""

import asyncio
import json
import logging
import os
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage
import pytest
import structlog

from shared.contracts.queues.po import (
    POInputMessage,
    POPayloadProtectionError,
    POReminderMessage,
    POSystemEvent,
    POUserMessage,
    protect_po_payload,
    to_flat_fields,
    unprotect_po_payload,
)
from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE, PO_REMINDERS_KEY
from shared.redis import RedisStreamClient
from src.consumers.po import _process_message

CANARIES = (
    "1234567890:AAredis_test_token_abcdefgh",
    "sk-or-v1-redis_test_provider_abcdefgh",
    "GOCSPX-redis_test_oauth_abcdefgh",
    "wqzj mvnp frtk bxhs",
)
SECRET_TEXT = " / ".join(CANARIES)
TEST_KEY = "wHhIQWmPfLt60oHdxzbQhY1ZKnUon12e5_SuZ33xDxc="
CHAT = "14310001"


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", TEST_KEY)
    result = RedisStreamClient(os.environ["REDIS_URL"], stream_maxlen=0)
    await result.connect()
    await result.redis.flushdb()
    yield result
    await result.redis.flushdb()
    await result.close()


@pytest.fixture(autouse=True)
def safe_logs(caplog, capsys, monkeypatch):
    from shared.redis import client as redis_client
    from src.agents.po import reminders, tools, tools_notices
    from src.consumers import po

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
    for module in (tools, tools_notices, reminders, po, redis_client):
        monkeypatch.setattr(module, "logger", structlog.get_logger(module.__name__))
    caplog.set_level(logging.DEBUG)
    yield events
    structlog.configure(**previous)
    captured = capsys.readouterr()
    assert events, "the log capture must exercise emitted diagnostics"
    assert_no_secrets(str(events) + caplog.text + captured.out + captured.err)


def assert_no_secrets(value):
    rendered = str(value)
    for secret in CANARIES:
        assert secret not in rendered


async def next_input(client, *, consumer="test", pending_timeout_ms=0):
    reader = client.consume_typed(
        PO_INPUT_QUEUE,
        PO_CONSUMER_GROUP,
        consumer,
        POInputMessage,
        block_ms=5,
        pending_timeout_ms=pending_timeout_ms,
    )
    try:
        async with asyncio.timeout(3):
            async for message in reader:
                if message is not None:
                    return message
    finally:
        await reader.aclose()


async def test_input_reply_echo_reclaim_and_retained_ack(client, monkeypatch):
    message = POUserMessage(
        text=SECRET_TEXT,
        telegram_chat_id=CHAT,
        request_id="request-1",
        user_name=SECRET_TEXT,
    )
    entry_id = await client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(message))
    assert_no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))
    first = await next_input(client)
    assert first.value == message
    reclaimed = await next_input(client, consumer="restarted")
    assert reclaimed.message_id == first.message_id == entry_id
    graph = AsyncMock()
    graph.aget_state.return_value.values = {"messages": []}
    graph.ainvoke.return_value = {"messages": [AIMessage(content=SECRET_TEXT)]}
    monkeypatch.setattr("src.consumers.po._record_user_message", AsyncMock())
    await _process_message(graph, client, asyncio.Semaphore(1), {}, entry_id, reclaimed.value)
    assert (await client.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 0
    assert_no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))
    assert_no_secrets(await client.redis.xrange("po:response:request-1"))
    assert SECRET_TEXT in str(graph.ainvoke.call_args)


async def test_reminder_storage_fire_proactive_and_latest_owner_cache(client, monkeypatch):
    from src.agents.po import tools_notices, tools_shared
    from src.agents.po.reminders import _poll_once
    from src.agents.po.tools import notify_user, set_reminder
    from src.agents.po.tools_notices import (
        latest_notice_key,
        remember_owner_event,
        suppress_owner_notice,
    )

    monkeypatch.setattr(tools_shared, "_stream_client", client)
    config = {"configurable": {"telegram_chat_id": CHAT, "user_turn": True}}
    await set_reminder.ainvoke(
        {"delay_minutes": 0, "reason": SECRET_TEXT, "story_id": SECRET_TEXT}, config=config
    )
    assert_no_secrets(await client.redis.zrange(PO_REMINDERS_KEY, 0, -1, withscores=True))
    assert await _poll_once(client) == 1
    assert await client.redis.zcard(PO_REMINDERS_KEY) == 0
    assert_no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))
    reminder = await next_input(client)
    assert reminder.value.text == SECRET_TEXT
    assert reminder.value.user_requested
    await notify_user.ainvoke({"message": SECRET_TEXT}, config=config)
    assert_no_secrets(await client.redis.xrange(PO_PROACTIVE_QUEUE))
    event = POSystemEvent(
        text=SECRET_TEXT,
        event="story_completed",
        telegram_chat_id=CHAT,
        story_id="story-1",
    )
    await remember_owner_event(client.redis, CHAT, event.model_dump(mode="json"))
    key = latest_notice_key(CHAT, "story-1")
    raw = await client.redis.get(key)
    assert_no_secrets(raw)
    assert POSystemEvent.model_validate(unprotect_po_payload(key, json.loads(raw))) == event
    monkeypatch.setattr(tools_notices, "_owns_story", AsyncMock(return_value=True))
    reader = AsyncMock()
    reader.get_product_brief_by_story.return_value.confirmed_at = "confirmed"
    monkeypatch.setattr(tools_notices, "ApiSituationReader", lambda *a: reader)
    monkeypatch.setattr(tools_notices, "_get_api", lambda: AsyncMock())
    assert "no durable record" in await suppress_owner_notice.ainvoke(
        {"story_id": "story-1", "reason": "defer"},
        config=config,
    )


@pytest.mark.parametrize(
    "bad", ["corrupt", "wrong_key", "malformed", "plaintext", "legacy", "binary"]
)
async def test_poison_quarantine_encrypts_entire_evidence_before_ack(client, monkeypatch, bad):
    from shared.contracts.queues.po import PO_PAYLOAD_ENVELOPE

    data = to_flat_fields(POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="bad"))
    if bad == "malformed":
        data["telegram_chat_id"] = {"echo": SECRET_TEXT}
    if bad == "legacy":
        data["user_id"] = SECRET_TEXT
        data["story_id"] = SECRET_TEXT
    fields = protect_po_payload(PO_INPUT_QUEUE, data)
    if bad == "corrupt":
        fields[PO_PAYLOAD_ENVELOPE] = fields[PO_PAYLOAD_ENVELOPE][:-5] + "bad!!"
    if bad == "wrong_key":
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        fields = protect_po_payload(PO_INPUT_QUEUE, data)
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", TEST_KEY)
    if bad == "plaintext":
        fields = data
    if bad == "binary":
        fields = {b"bad\xff": SECRET_TEXT.encode()}
    alert = AsyncMock()
    monkeypatch.setattr("shared.notifications.notify_admins_best_effort", alert)
    entry_id = await client.redis.xadd(PO_INPUT_QUEUE, fields)
    reader = client.consume_typed(
        PO_INPUT_QUEUE,
        PO_CONSUMER_GROUP,
        "poison",
        POInputMessage,
        block_ms=5,
    )
    try:
        async with asyncio.timeout(3):
            assert await anext(reader) is None
    finally:
        await reader.aclose()
    assert (await client.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 0
    dlq = await client.redis.xrange("po:input:dlq")
    assert len(dlq) == 1
    assert_no_secrets(dlq)
    assert_no_secrets(alert.call_args_list)
    logical = unprotect_po_payload("po:input:dlq", dlq[0][1])
    assert logical["entry_id"] == entry_id
    if bad in ("malformed", "legacy"):
        assert_no_secrets(logical)
        assert unprotect_po_payload(PO_INPUT_QUEUE, json.loads(logical["body"])) == data


@pytest.mark.parametrize("key", [None, "invalid-key"])
async def test_invalid_config_refuses_all_writes_and_keeps_pending(client, monkeypatch, key):
    from src.agents.po import tools_shared
    from src.agents.po.tools import set_reminder
    from src.agents.po.tools_notices import OwnerNoticeReadUnknown, remember_owner_event

    valid = protect_po_payload(
        PO_INPUT_QUEUE,
        to_flat_fields(
            POUserMessage(
                text=SECRET_TEXT,
                telegram_chat_id=CHAT,
                request_id="pending",
            )
        ),
    )
    await client.redis.xadd(PO_INPUT_QUEUE, valid)
    if key is None:
        monkeypatch.delenv("SECRETS_ENCRYPTION_KEY")
    else:
        monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", key)
    with pytest.raises(POPayloadProtectionError) as failure:
        await client.publish_flat(PO_PROACTIVE_QUEUE, {"text": SECRET_TEXT})
    assert_no_secrets(str(failure.value))
    monkeypatch.setattr(tools_shared, "_stream_client", client)
    with pytest.raises(POPayloadProtectionError):
        await set_reminder.ainvoke(
            {"delay_minutes": 0, "reason": SECRET_TEXT},
            config={
                "configurable": {"telegram_chat_id": CHAT, "user_turn": True},
            },
        )
    with pytest.raises(OwnerNoticeReadUnknown):
        await remember_owner_event(
            client.redis,
            CHAT,
            POSystemEvent(
                event="story_completed",
                text=SECRET_TEXT,
                story_id="story-1",
                telegram_chat_id=CHAT,
            ).model_dump(mode="json"),
        )
    reader = client.consume_typed(
        PO_INPUT_QUEUE,
        PO_CONSUMER_GROUP,
        "bad-key",
        POInputMessage,
        block_ms=5,
    )
    try:
        async with asyncio.timeout(3):
            assert await anext(reader) is None
    finally:
        await reader.aclose()
    assert (await client.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 1
    assert not await client.redis.exists("po:input:dlq", PO_PROACTIVE_QUEUE, PO_REMINDERS_KEY)
    assert not await client.redis.exists("po:latest_owner_event:14310001:story-1")


async def test_handler_failure_returns_fixed_protected_error(client, monkeypatch):
    monkeypatch.setattr("src.consumers.po._record_user_message", AsyncMock())
    graph = AsyncMock()
    graph.aget_state.return_value.values = {"messages": []}
    graph.ainvoke.side_effect = RuntimeError(SECRET_TEXT)
    message = POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="fails")
    entry_id = await client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(message))
    incoming = await next_input(client)
    await _process_message(graph, client, asyncio.Semaphore(1), {}, entry_id, incoming.value)
    raw = (await client.redis.xrange("po:response:fails"))[0][1]
    assert_no_secrets(raw)
    error = unprotect_po_payload("po:response:fails", raw)
    assert error["error"] == "true"
    assert_no_secrets(error)


async def test_echoed_reminder_identifier_stays_pending_without_rendering_secrets(
    client, monkeypatch
):
    from src.consumers import po

    monkeypatch.setattr(
        po, "story_is_ordered", AsyncMock(side_effect=po.StoryAudienceUnknown(SECRET_TEXT))
    )
    message = POReminderMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, story_id=SECRET_TEXT)
    entry_id = await client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(message))
    incoming = await next_input(client)
    await _process_message(AsyncMock(), client, asyncio.Semaphore(1), {}, entry_id, incoming.value)
    assert (await client.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 1
    assert_no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))


async def test_startup_refuses_plaintext_without_touching_delivery_state(client):
    from shared.redis.po import verify_po_storage

    entry_id = await client.redis.xadd(PO_INPUT_QUEUE, {"text": SECRET_TEXT})
    await client.ensure_consumer_group(PO_INPUT_QUEUE, PO_CONSUMER_GROUP)
    before = await client.redis.dump(PO_INPUT_QUEUE)
    with pytest.raises(POPayloadProtectionError) as failure:
        await verify_po_storage(client.redis)
    assert_no_secrets(str(failure.value))
    assert await client.redis.dump(PO_INPUT_QUEUE) == before
    assert not await client.redis.exists("po:input:dlq")
    await client.redis.xdel(PO_INPUT_QUEUE, entry_id)
    await verify_po_storage(client.redis)


async def test_failed_encryption_refuses_before_redis_receives_any_value(client, monkeypatch):
    from src.agents.po import tools_shared
    from src.agents.po.tools import set_reminder
    from src.agents.po.tools_notices import OwnerNoticeReadUnknown, remember_owner_event

    def fail(*args):
        raise RuntimeError(SECRET_TEXT)

    monkeypatch.setattr("shared.crypto.SecretsCipher.encrypt", fail)
    monkeypatch.setattr(tools_shared, "_stream_client", client)
    with pytest.raises(POPayloadProtectionError) as failure:
        await client.publish_flat(PO_INPUT_QUEUE, {"text": SECRET_TEXT})
    assert_no_secrets(str(failure.value))
    with pytest.raises(POPayloadProtectionError):
        await set_reminder.ainvoke(
            {"delay_minutes": 0, "reason": SECRET_TEXT},
            config={
                "configurable": {"telegram_chat_id": CHAT, "user_turn": True},
            },
        )
    with pytest.raises(OwnerNoticeReadUnknown):
        await remember_owner_event(
            client.redis,
            CHAT,
            POSystemEvent(
                event="story_completed",
                text=SECRET_TEXT,
                story_id="story-1",
                telegram_chat_id=CHAT,
            ).model_dump(mode="json"),
        )
    assert await client.redis.dbsize() == 0


async def test_real_dlq_permission_failure_keeps_entry_pending_until_reclaim(client):
    from redis.asyncio import Redis

    await client.redis.execute_command(
        "ACL",
        "SETUSER",
        "po-no-dlq",
        "reset",
        "on",
        "nopass",
        "+@all",
        "~po:input",
        "~stream:diagnostics:lost_entries",
    )
    restricted = RedisStreamClient(os.environ["REDIS_URL"], stream_maxlen=0)
    restricted._redis = Redis.from_url(
        os.environ["REDIS_URL"], username="po-no-dlq", decode_responses=True
    )
    try:
        await client.publish_flat(PO_INPUT_QUEUE, {"type": "bad", "text": SECRET_TEXT})
        reader = restricted.consume_typed(
            PO_INPUT_QUEUE, PO_CONSUMER_GROUP, "denied", POInputMessage, block_ms=5
        )
        try:
            assert await anext(reader) is None
        finally:
            await reader.aclose()
        assert (await client.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 1
        assert not await client.redis.exists("po:input:dlq")
        retry = client.consume_typed(
            PO_INPUT_QUEUE,
            PO_CONSUMER_GROUP,
            "restarted",
            POInputMessage,
            block_ms=5,
            pending_timeout_ms=0,
        )
        try:
            assert await anext(retry) is None
        finally:
            await retry.aclose()
        assert (await client.redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 0
        assert_no_secrets(await client.redis.xrange("po:input:dlq"))
    finally:
        await restricted.close()
        await client.redis.execute_command("ACL", "DELUSER", "po-no-dlq")


async def test_trusted_stand_entrypoints_protect_before_xadd(client):
    from shared.redis.po_cli import execute

    message = to_flat_fields(
        POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="stand")
    )
    await execute(
        client.redis,
        ["XADD", PO_INPUT_QUEUE, "*", *[item for pair in message.items() for item in pair]],
    )
    assert_no_secrets(await client.redis.xrange(PO_INPUT_QUEUE))
    logical = await execute(client.redis, ["XRANGE", PO_INPUT_QUEUE, "-", "+"])
    assert dict(zip(logical[0][1][::2], logical[0][1][1::2], strict=True)) == message


async def test_deploy_write_fence_drops_uncommitted_work_when_clients_stop(client):
    from redis.asyncio import Redis

    producer = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    producer_id = await producer.client_id()
    await client.redis.execute_command("CLIENT", "PAUSE", 10000, "WRITE")
    blocked = asyncio.create_task(producer.xadd(PO_INPUT_QUEUE, {"text": SECRET_TEXT}))
    try:
        # Prove the command arrived but cannot persist, before stopping this producer.
        async with asyncio.timeout(2):
            while not any(
                c["id"] == str(producer_id) and c["cmd"] == "xadd"
                for c in await client.redis.client_list()
            ):
                await asyncio.sleep(0)
        assert not blocked.done()
        assert not await client.redis.exists(PO_INPUT_QUEUE)
        await client.redis.client_kill_filter(_id=producer_id)
        blocked.cancel()
        await asyncio.gather(blocked, return_exceptions=True)
    finally:
        await producer.aclose()
        await client.redis.execute_command("CLIENT", "UNPAUSE")
    assert not await client.redis.exists(PO_INPUT_QUEUE)
