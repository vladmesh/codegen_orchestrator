"""Unit tests for shared.redis.client.RedisStreamClient."""

import json
from unittest.mock import patch

from fakeredis import aioredis
import pytest

from shared.contracts.base import BaseMessage
from shared.redis.client import RedisStreamClient, StreamMessage


class SampleMessage(BaseMessage):
    """Minimal BaseMessage subclass for testing."""

    content: str = "test"


@pytest.fixture
async def fake_redis():
    r = aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def client(fake_redis):
    c = RedisStreamClient(redis_url="redis://fake:6379")
    c._redis = fake_redis
    return c


class TestInit:
    def test_raises_without_url_and_env(self, monkeypatch):
        monkeypatch.delenv("REDIS_URL", raising=False)
        with pytest.raises(RuntimeError, match="Redis URL not provided"):
            RedisStreamClient()

    def test_accepts_explicit_url(self):
        c = RedisStreamClient(redis_url="redis://localhost:6379")
        assert c.redis_url == "redis://localhost:6379"

    def test_falls_back_to_env_var(self, monkeypatch):
        monkeypatch.setenv("REDIS_URL", "redis://from-env:6379")
        c = RedisStreamClient()
        assert c.redis_url == "redis://from-env:6379"


class TestConnection:
    async def test_connect_creates_redis_client(self):
        c = RedisStreamClient(redis_url="redis://fake:6379")
        assert c._redis is None
        with patch(
            "shared.redis.client.redis.from_url",
            return_value=aioredis.FakeRedis(decode_responses=True),
        ):
            await c.connect()
        assert c._redis is not None

    async def test_connect_is_idempotent(self):
        c = RedisStreamClient(redis_url="redis://fake:6379")
        with patch(
            "shared.redis.client.redis.from_url",
            return_value=aioredis.FakeRedis(decode_responses=True),
        ) as mock:
            await c.connect()
            await c.connect()
        mock.assert_called_once()

    async def test_property_raises_before_connect(self):
        c = RedisStreamClient(redis_url="redis://fake:6379")
        with pytest.raises(RuntimeError, match="not connected"):
            _ = c.redis

    async def test_close_disconnects(self, client):
        await client.close()
        assert client._redis is None


class TestPublish:
    async def test_returns_message_id(self, client):
        msg_id = await client.publish("test:stream", {"key": "value"})
        assert msg_id is not None

    async def test_data_stored_as_json_in_data_field(self, client, fake_redis):
        await client.publish("test:stream", {"key": "value", "num": 42})
        messages = await fake_redis.xrange("test:stream")
        assert len(messages) == 1
        data = json.loads(messages[0][1]["data"])
        assert data == {"key": "value", "num": 42}

    async def test_publish_message_serializes_pydantic_model(self, client, fake_redis):
        msg = SampleMessage(content="hello")
        await client.publish_message("test:stream", msg)
        messages = await fake_redis.xrange("test:stream")
        data = json.loads(messages[0][1]["data"])
        assert data["content"] == "hello"


class TestConsumerGroup:
    async def test_creates_group(self, client, fake_redis):
        await client.ensure_consumer_group("test:stream", "test-group")
        info = await fake_redis.xinfo_groups("test:stream")
        assert any(g["name"] == "test-group" for g in info)

    async def test_duplicate_group_does_not_raise(self, client):
        await client.ensure_consumer_group("test:stream", "test-group")
        await client.ensure_consumer_group("test:stream", "test-group")


class TestConsume:
    async def test_receives_published_message(self, client):
        await client.publish("s", {"key": "val"})
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is not None:
                assert isinstance(msg, StreamMessage)
                assert msg.data == {"key": "val"}
                break

    async def test_message_auto_acked(self, client, fake_redis):
        await client.publish("s", {"data": "x"})
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is not None:
                continue  # resume generator so xack executes after yield
            break  # None = idle, all messages processed
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0

    async def test_yields_none_on_empty_stream(self, client):
        got_none = False
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is None:
                got_none = True
            break
        assert got_none

    async def test_invalid_json_acked_and_skipped(self, client, fake_redis):
        await fake_redis.xadd("s", {"data": "{invalid"})
        received = []
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is not None:
                received.append(msg)
            break
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0
        assert len(received) == 0
