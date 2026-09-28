"""Released plaintext fixtures and the quiesced Redis conversion boundary."""

import asyncio
from datetime import UTC, datetime
import json
import time
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage
import pytest

from shared.contracts.dto.owner_notification import OwnerNoticeReference
from shared.contracts.queues.po import (
    POReminderMessage,
    POSystemEvent,
    POUserMessage,
    to_flat_fields,
    unprotect_po_payload,
)
from shared.queues import (
    PO_CONSUMER_GROUP,
    PO_INPUT_QUEUE,
    PO_PROACTIVE_GROUP,
    PO_PROACTIVE_QUEUE,
    PO_REMINDERS_KEY,
)
from shared.redis import RedisStreamClient
from src.agents.po.tools_notices import latest_notice_key
from src.consumers.po import _process_message

from .test_po_redis_secrets import CHAT, SECRET_TEXT, assert_no_secrets


class ReleasedFlatWriter(RedisStreamClient):
    """The released publish_flat implementation, isolated to old-state drain fixtures."""

    async def publish_flat(self, stream, fields):
        return await self.redis.xadd(stream, fields, **self._xadd_kwargs())


async def released_state(redis):
    message = POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="old-request")
    await redis.xadd(PO_INPUT_QUEUE, to_flat_fields(message), id="100-1")
    await redis.xgroup_create(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, id="0")
    await redis.xreadgroup(PO_CONSUMER_GROUP, "old-po", {PO_INPUT_QUEUE: ">"})
    await redis.xack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, "100-1")
    await redis.xadd(
        PO_PROACTIVE_QUEUE, {"text": SECRET_TEXT, "telegram_chat_id": CHAT}, id="101-1"
    )
    await redis.xgroup_create(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, id="0")
    await redis.xreadgroup(PO_PROACTIVE_GROUP, "old-bot", {PO_PROACTIVE_QUEUE: ">"})
    await redis.xack(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, "101-1")
    await redis.xadd(
        "po:response:old-request", {"text": SECRET_TEXT, "telegram_chat_id": CHAT}, id="102-1"
    )
    await redis.pexpire("po:response:old-request", 600000)
    dlq = {
        "source_stream": PO_INPUT_QUEUE,
        "group": PO_CONSUMER_GROUP,
        "entry_id": "99-1",
        "failure": "validation_error",
        "reason": json.dumps({"echo": SECRET_TEXT}),
        "quarantined_at": "2026-09-28T00:00:00+00:00",
        "body": json.dumps({"text": SECRET_TEXT}),
    }
    await redis.xadd("po:input:dlq", dlq, id="103-1")
    reminder = POReminderMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, user_requested=True)
    await redis.zadd(PO_REMINDERS_KEY, {reminder.model_dump_json(): time.time() + 3600})
    event = POSystemEvent(
        event="story_completed",
        text=SECRET_TEXT,
        story_id="story-1",
        telegram_chat_id=CHAT,
        owner_notice=OwnerNoticeReference(
            source="story", source_id="story-1", owed_at=datetime.now(UTC)
        ),
    )
    key = latest_notice_key(CHAT, "story-1")
    await redis.set(key, event.model_dump_json())
    return message, reminder, event, key, dlq


async def test_upgrade_preserves_ids_groups_scores_ttls_and_notice_data(client):
    from src.agents.po.redis_upgrade import upgrade

    redis = client.redis
    message, reminder, event, key, dlq = await released_state(redis)
    expires = await redis.pexpiretime("po:response:old-request")
    score = (await redis.zrange(PO_REMINDERS_KEY, 0, -1, withscores=True))[0][1]
    info = await redis.xinfo_stream(PO_INPUT_QUEUE)
    group = await redis.xinfo_groups(PO_INPUT_QUEUE)
    dry = await upgrade(redis, writers_quiesced=True)
    assert sum(dry["would_convert"].values()) == 6
    assert (await redis.xrange(PO_INPUT_QUEUE))[0][1] == to_flat_fields(message)
    applied = await upgrade(redis, writers_quiesced=True, apply=True)
    assert sum(applied["converted"].values()) == 6
    assert all(counts["plaintext"] == 0 for counts in applied["after"].values())
    for stream in (PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE, "po:response:old-request", "po:input:dlq"):
        assert_no_secrets(await redis.xrange(stream))
    entries = await redis.xrange(PO_INPUT_QUEUE)
    assert entries[0][0] == "100-1"
    assert unprotect_po_payload(PO_INPUT_QUEUE, entries[0][1]) == to_flat_fields(message)
    assert unprotect_po_payload("po:input:dlq", (await redis.xrange("po:input:dlq"))[0][1]) == dlq
    after_info = await redis.xinfo_stream(PO_INPUT_QUEUE)
    for field in ("last-generated-id", "entries-added", "max-deleted-entry-id", "length"):
        assert after_info[field] == info[field]
    assert await redis.xinfo_groups(PO_INPUT_QUEUE) == group
    assert await redis.pexpiretime("po:response:old-request") == expires
    members = await redis.zrange(PO_REMINDERS_KEY, 0, -1, withscores=True)
    assert_no_secrets(members)
    assert members[0][1] == score
    assert unprotect_po_payload(PO_REMINDERS_KEY, json.loads(members[0][0])) == reminder.model_dump(
        mode="json"
    )
    raw_event = await redis.get(key)
    assert_no_secrets(raw_event)
    assert unprotect_po_payload(key, json.loads(raw_event)) == event.model_dump(mode="json")
    assert sum((await upgrade(redis, writers_quiesced=True, apply=True))["converted"].values()) == 0


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "unread",
        "missing_group",
        "unknown_group",
        "wrong_type",
        "bad_reminder",
        "bad_event",
        "bad_ciphertext",
        "not_quiesced",
    ],
)
async def test_upgrade_refuses_before_mutation(client, state):
    from src.agents.po.redis_upgrade import PORedisUpgradeError, upgrade

    redis = client.redis
    await released_state(redis)
    if state == "pending":
        await redis.xadd(PO_INPUT_QUEUE, {"text": SECRET_TEXT})
        await redis.xreadgroup(PO_CONSUMER_GROUP, "old-po", {PO_INPUT_QUEUE: ">"})
    elif state == "unread":
        await redis.xadd(PO_INPUT_QUEUE, {"text": SECRET_TEXT})
    elif state == "missing_group":
        await redis.xgroup_destroy(PO_INPUT_QUEUE, PO_CONSUMER_GROUP)
    elif state == "unknown_group":
        await redis.xgroup_create(PO_INPUT_QUEUE, "unknown", id="$")
    elif state == "wrong_type":
        await redis.set("po:response:bad", SECRET_TEXT)
    elif state == "bad_reminder":
        await redis.zadd(PO_REMINDERS_KEY, {SECRET_TEXT: 42})
    elif state == "bad_event":
        await redis.set(latest_notice_key(CHAT, "story-2"), json.dumps({"text": SECRET_TEXT}))
    elif state == "bad_ciphertext":
        await redis.xadd("po:response:bad", {"po_encrypted_v1": SECRET_TEXT})
    before = {k: await redis.dump(k) for k in [k async for k in redis.scan_iter()]}
    with pytest.raises(PORedisUpgradeError) as failure:
        await upgrade(redis, writers_quiesced=state != "not_quiesced", apply=True)
    assert_no_secrets(str(failure.value))
    assert {k: await redis.dump(k) for k in [k async for k in redis.scan_iter()]} == before


async def test_old_consumer_drains_pending_work_before_conversion_and_reminder_survives(
    client, monkeypatch
):
    from src.agents.po.redis_upgrade import PORedisUpgradeError, upgrade
    from src.agents.po.reminders import _poll_once

    from .test_po_redis_secrets import next_input

    redis = client.redis
    await released_state(redis)
    message = POUserMessage(text=SECRET_TEXT, telegram_chat_id=CHAT, request_id="still-owed")
    await redis.xadd(PO_INPUT_QUEUE, to_flat_fields(message))
    handed = await redis.xreadgroup(PO_CONSUMER_GROUP, "old-po", {PO_INPUT_QUEUE: ">"})
    entry_id, fields = handed[0][1][0]
    with pytest.raises(PORedisUpgradeError):
        await upgrade(redis, writers_quiesced=True, apply=True)
    old_client = ReleasedFlatWriter(client.redis_url, stream_maxlen=0)
    old_client._redis = redis
    graph = AsyncMock()
    graph.aget_state.return_value.values = {"messages": []}
    graph.ainvoke.return_value = {"messages": [AIMessage(content=SECRET_TEXT)]}
    monkeypatch.setattr("src.consumers.po._record_user_message", AsyncMock())
    await _process_message(
        graph, old_client, asyncio.Semaphore(1), {}, entry_id, POUserMessage.model_validate(fields)
    )
    # Work was delivered, and the old wire response remains available to its waiter.
    response = await redis.xread({"po:response:still-owed": "0"})
    assert response[0][1][0][1]["text"] == SECRET_TEXT
    assert (await redis.xpending(PO_INPUT_QUEUE, PO_CONSUMER_GROUP))["pending"] == 0
    report = await upgrade(redis, writers_quiesced=True, apply=True)
    assert report["after"]["input"]["protected"] == 2
    assert report["after"]["response"]["protected"] == 2
    assert report["after"]["reminders"]["protected"] == 1
    assert_no_secrets(await redis.xrange("po:response:still-owed"))
    # Preserve the fire score during conversion; later fire through the real poller.
    fire_at = (await redis.zrange(PO_REMINDERS_KEY, 0, -1, withscores=True))[0][1]
    monkeypatch.setattr("src.agents.po.reminders.time.time", lambda: fire_at + 1)
    assert await _poll_once(client) == 1
    reminder = await next_input(client)
    assert reminder.value.text == SECRET_TEXT and reminder.value.user_requested
    assert_no_secrets(await redis.xrange(PO_INPUT_QUEUE))
