"""A notification the bot died while sending is picked up by the next bot.

The listener consumes without auto-ack, so an entry whose delivery never
finished stays in the group's pending list. This walks the real restart path:
Redis hands the entry out, the consumer disappears without settling it, a fresh
``ProactiveListener`` starts, and the message reaches the chat it was addressed
to. Without the claim of pending entries the notification would sit in the PEL
forever — delivered to nobody, retried never, alerted about never.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from fakeredis.aioredis import FakeRedis
import pytest
import pytest_asyncio

from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.queues import PO_PROACTIVE_GROUP, PO_PROACTIVE_QUEUE
from shared.redis.client import RedisStreamClient
from src.main import ProactiveListener
from src.proactive import PROACTIVE_MAX_DELIVERIES

OWNER_CHAT_ID = 987654321
INTERNAL_USER_ID = 1


def _fields() -> dict[str, str]:
    return to_flat_fields(
        POProactiveMessage(
            text="Your project is deployed",
            telegram_chat_id=str(OWNER_CHAT_ID),
            owner_user_id=str(INTERNAL_USER_ID),
            event="story_completed",
            story_id="story-7",
            project_id="proj-3",
        )
    )


@pytest_asyncio.fixture
async def client():
    stream_client = RedisStreamClient(redis_url="redis://fake:6379")
    stream_client._redis = FakeRedis(decode_responses=True)
    yield stream_client
    await stream_client.close()


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr("src.proactive.PROACTIVE_RETRY_DELAY_S", 0)


async def _abandon_one_delivery(client: RedisStreamClient) -> None:
    """Hand the entry to the group and never settle it, as a killed bot does."""
    await client.redis.xreadgroup(
        groupname=PO_PROACTIVE_GROUP,
        consumername="bot-0",
        streams={PO_PROACTIVE_QUEUE: ">"},
        count=10,
        block=0,
    )


async def _pending_count(client: RedisStreamClient) -> int:
    entries = await client.redis.xpending_range(
        PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, min="-", max="+", count=10
    )
    return len(entries)


async def _run_listener_until(client: RedisStreamClient, bot, condition) -> None:
    """Run one listener incarnation until *condition* holds, then stop it."""
    listener = ProactiveListener(client=client)
    task = await listener.start(bot)
    try:
        for _ in range(200):
            if condition():
                break
            await asyncio.sleep(0.01)
    finally:
        await listener.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_a_delivery_abandoned_by_a_restart_reaches_the_owners_chat(client):
    await client.ensure_consumer_group(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP)
    await client.publish_flat(PO_PROACTIVE_QUEUE, _fields())
    await _abandon_one_delivery(client)
    assert await _pending_count(client) == 1, "the killed bot left the entry pending"

    bot = AsyncMock()
    await _run_listener_until(client, bot, lambda: bot.send_message.await_count > 0)

    assert bot.send_message.await_count >= 1
    chat_id = bot.send_message.await_args.kwargs["chat_id"]
    assert chat_id == OWNER_CHAT_ID
    # The regression the whole card exists for: the internal id is not a chat.
    assert chat_id != INTERNAL_USER_ID
    assert await _pending_count(client) == 0, "a delivered entry is acked"


@pytest.mark.asyncio
async def test_repeated_restarts_end_in_an_admin_alert_instead_of_looping(client):
    """The bound survives restarts, so a message that keeps killing the bot stops."""
    await client.ensure_consumer_group(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP)
    await client.publish_flat(PO_PROACTIVE_QUEUE, _fields())
    for _ in range(PROACTIVE_MAX_DELIVERIES + 1):
        await _abandon_one_delivery(client)
        await client.redis.xautoclaim(
            PO_PROACTIVE_QUEUE,
            PO_PROACTIVE_GROUP,
            "bot-0",
            min_idle_time=0,
            start_id="0-0",
            count=10,
        )

    bot = AsyncMock()
    with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()) as alert:
        await _run_listener_until(client, bot, lambda: alert.await_count > 0)

    alert.assert_awaited_once()
    text = alert.await_args.args[0]
    assert "story-7" in text
    assert "proj-3" in text
    bot.send_message.assert_not_awaited()
    assert await _pending_count(client) == 0, "the entry is settled, not retried forever"
