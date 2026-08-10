"""Delivery contract for PO proactive notifications.

A notification the user never received has to be visible: delivery retries a
bounded number of times, and exhaustion raises an admin alert carrying the
identifiers needed to find the story it belonged to. The stream side is a real
``RedisStreamClient`` over fakeredis, so the ack and the pending-entry
bookkeeping the bound relies on are the real ones, not a mock's answer.
"""

from unittest.mock import AsyncMock, patch

from fakeredis.aioredis import FakeRedis
import pytest
import pytest_asyncio

from shared.contracts.queues.po import POProactiveMessage, to_flat_fields
from shared.queues import PO_PROACTIVE_GROUP, PO_PROACTIVE_QUEUE
from shared.redis.client import RedisStreamClient
from src.proactive import (
    PROACTIVE_MAX_ATTEMPTS,
    PROACTIVE_MAX_DELIVERIES,
    ProactiveOutcome,
    attempt_proactive_delivery,
    process_proactive_entry,
)

OWNER_CHAT_ID = 987654321


def _message(**overrides) -> POProactiveMessage:
    fields = {
        "text": "Your project is deployed",
        "telegram_chat_id": str(OWNER_CHAT_ID),
        "owner_user_id": "1",
        "event": "story_completed",
        "story_id": "story-7",
        "project_id": "proj-3",
    }
    fields.update(overrides)
    return POProactiveMessage(**fields)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """Retries back off in production; the test does not have to wait for it."""
    monkeypatch.setattr("src.proactive.PROACTIVE_RETRY_DELAY_S", 0)


@pytest_asyncio.fixture
async def client():
    stream_client = RedisStreamClient(redis_url="redis://fake:6379")
    stream_client._redis = FakeRedis(decode_responses=True)
    yield stream_client
    await stream_client.close()


async def _publish_and_read(client: RedisStreamClient, fields: dict[str, str]):
    """Put *fields* on po:proactive and hand it to the group, as the listener would."""
    await client.ensure_consumer_group(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP)
    await client.publish_flat(PO_PROACTIVE_QUEUE, fields)
    async for msg in client.consume(
        PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, "bot-0", count=1, auto_ack=False
    ):
        if msg is not None:
            return msg
    raise AssertionError("nothing to consume")


async def _pending_ids(client: RedisStreamClient) -> list[str]:
    entries = await client.redis.xpending_range(
        PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, min="-", max="+", count=10
    )
    return [entry["message_id"] for entry in entries]


class TestBoundedRetry:
    @pytest.mark.asyncio
    async def test_delivered_on_first_attempt_sends_once(self):
        bot = AsyncMock()

        error = await attempt_proactive_delivery(bot, _message())

        assert error is None
        assert bot.send_message.await_count == 1
        assert bot.send_message.await_args.kwargs["chat_id"] == OWNER_CHAT_ID

    @pytest.mark.asyncio
    async def test_transient_failure_is_retried_and_then_delivered(self):
        bot = AsyncMock()
        # Every attempt tries HTML first and falls back to plain text, so a
        # failing attempt costs two calls. The first attempt fails both ways.
        bot.send_message.side_effect = [
            Exception("telegram 502"),
            Exception("telegram 502"),
            None,
        ]

        assert await attempt_proactive_delivery(bot, _message()) is None

    @pytest.mark.asyncio
    async def test_permanent_failure_stops_after_max_attempts(self):
        bot = AsyncMock()
        bot.send_message.side_effect = Exception("chat not found")

        error = await attempt_proactive_delivery(bot, _message())

        assert error is not None
        # Two calls per attempt (HTML, then the plain-text fallback), and the
        # loop stops instead of retrying forever.
        assert bot.send_message.await_count == PROACTIVE_MAX_ATTEMPTS * 2


class TestEntryOutcomes:
    @pytest.mark.asyncio
    async def test_delivered_entry_is_acked_and_raises_no_alert(self, client):
        bot = AsyncMock()
        msg = await _publish_and_read(client, to_flat_fields(_message()))

        with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()) as alert:
            outcome = await process_proactive_entry(bot, client, msg)

        assert outcome is ProactiveOutcome.DELIVERED
        assert bot.send_message.await_args.kwargs["chat_id"] == OWNER_CHAT_ID
        alert.assert_not_awaited()
        assert await _pending_ids(client) == []

    @pytest.mark.asyncio
    async def test_exhaustion_alerts_admins_with_identifiers_and_stops(self, client):
        bot = AsyncMock()
        bot.send_message.side_effect = Exception("chat not found")
        msg = await _publish_and_read(client, to_flat_fields(_message()))

        with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()) as alert:
            outcome = await process_proactive_entry(bot, client, msg)

        assert outcome is ProactiveOutcome.EXHAUSTED
        alert.assert_awaited_once()
        text = alert.await_args.args[0]
        assert "story-7" in text
        assert "proj-3" in text
        assert "story_completed" in text
        assert str(OWNER_CHAT_ID) in text
        assert alert.await_args.kwargs["level"] == "error"
        # Given up on, so it is not left pending to be retried forever.
        assert await _pending_ids(client) == []

    @pytest.mark.asyncio
    async def test_an_entry_past_the_delivery_bound_is_not_attempted_again(self, client):
        """The bound holds across restarts: the count lives in the PEL, not in memory."""
        bot = AsyncMock()
        msg = await _publish_and_read(client, to_flat_fields(_message()))

        # Every claim is another delivery, which is what a consumer dying mid-send
        # and being restarted looks like to Redis.
        for _ in range(PROACTIVE_MAX_DELIVERIES):
            await client.redis.xautoclaim(
                PO_PROACTIVE_QUEUE,
                PO_PROACTIVE_GROUP,
                "bot-0",
                min_idle_time=0,
                start_id="0-0",
                count=10,
            )

        with patch("src.proactive.notify_admins_best_effort", new=AsyncMock()) as alert:
            outcome = await process_proactive_entry(bot, client, msg)

        assert outcome is ProactiveOutcome.EXHAUSTED
        bot.send_message.assert_not_awaited()
        alert.assert_awaited_once()
        assert "story-7" in alert.await_args.args[0]
        assert await _pending_ids(client) == []


class TestLegacyAddressing:
    @pytest.mark.asyncio
    async def test_a_message_addressed_by_the_removed_user_id_is_refused_loudly(self, client):
        """The old field meant two different numbers; accepting it delivers to neither."""
        bot = AsyncMock()
        msg = await _publish_and_read(
            client,
            {
                "text": "Your project is deployed",
                "user_id": "1",
                "event": "story_completed",
                "story_id": "story-7",
                "project_id": "proj-3",
            },
        )

        # The rejection alert is raised through the shared notifications channel,
        # which ``shared.contracts.recipient`` imports at call time.
        with patch("shared.notifications.notify_admins_best_effort", new=AsyncMock()) as alert:
            outcome = await process_proactive_entry(bot, client, msg)

        assert outcome is ProactiveOutcome.REJECTED
        bot.send_message.assert_not_awaited()
        alert.assert_awaited_once()
        text = alert.await_args.args[0]
        assert "user_id" in text
        assert "story-7" in text
        assert "proj-3" in text
        assert await _pending_ids(client) == []
