"""Service tests for PO reminder flow (real Redis)."""

from __future__ import annotations

import json
import time

from fakeredis.aioredis import FakeRedis
import pytest

from shared.queues import PO_INPUT_QUEUE, PO_REMINDERS_KEY
from shared.redis_client import RedisStreamClient
from src.agents.po.reminders import _poll_once


@pytest.fixture
async def raw_redis():
    """Provides a clean FakeRedis client for each test."""
    client = FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def redis(raw_redis):
    """RedisStreamClient backed by FakeRedis."""
    client = RedisStreamClient(redis_url="redis://fake:6379")
    client._redis = raw_redis
    return client


@pytest.mark.asyncio
async def test_reminder_fires_and_reaches_po_input(redis, raw_redis):
    """E2E: ZADD reminder -> poller fires -> message appears in po:input."""
    # 1. Write a reminder that's already due (fire_at = now - 10)
    reminder = json.dumps(
        {
            "type": "reminder",
            "user_id": "user-42",
            "text": "check task eng-abc123",
            "timestamp": "2026-02-15T14:30:00+00:00",
        }
    )
    await raw_redis.zadd(PO_REMINDERS_KEY, {reminder: time.time() - 10})

    # 2. Create consumer group on po:input
    try:
        await raw_redis.xgroup_create(PO_INPUT_QUEUE, "test-group", id="0", mkstream=True)
    except Exception:  # noqa: S110
        pass

    # 3. Run one poll cycle
    fired = await _poll_once(redis)

    # 4. Verify reminder appeared in po:input
    entries = await raw_redis.xreadgroup("test-group", "t1", {PO_INPUT_QUEUE: ">"}, count=10)
    assert len(entries) == 1
    _, messages = entries[0]
    assert len(messages) == 1
    _, data = messages[0]
    assert data["type"] == "reminder"
    assert data["user_id"] == "user-42"
    assert data["text"] == "check task eng-abc123"

    # 5. Verify reminder removed from ZSET
    remaining = await raw_redis.zcard(PO_REMINDERS_KEY)
    assert remaining == 0

    # 6. Verify fired count
    assert fired == 1


@pytest.mark.asyncio
async def test_future_reminder_not_fired(redis, raw_redis):
    """Reminder with future timestamp should stay in ZSET."""
    reminder = json.dumps(
        {
            "type": "reminder",
            "user_id": "user-99",
            "text": "future check",
            "timestamp": "2026-02-15T20:00:00+00:00",
        }
    )
    await raw_redis.zadd(PO_REMINDERS_KEY, {reminder: time.time() + 3600})

    fired = await _poll_once(redis)

    assert fired == 0
    remaining = await raw_redis.zcard(PO_REMINDERS_KEY)
    assert remaining == 1


@pytest.mark.asyncio
async def test_multiple_due_reminders_all_fire(redis, raw_redis):
    """Multiple due reminders should all be moved to po:input."""
    now = time.time()
    for i in range(3):
        reminder = json.dumps(
            {
                "type": "reminder",
                "user_id": f"user-{i}",
                "text": f"check task {i}",
                "timestamp": "2026-02-15T14:30:00+00:00",
            }
        )
        await raw_redis.zadd(PO_REMINDERS_KEY, {reminder: now - (10 + i)})

    fired = await _poll_once(redis)

    assert fired == 3  # noqa: PLR2004
    remaining = await raw_redis.zcard(PO_REMINDERS_KEY)
    assert remaining == 0
