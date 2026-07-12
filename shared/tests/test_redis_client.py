"""Unit tests for shared.redis.client.RedisStreamClient."""

import json
from typing import Literal
from unittest.mock import AsyncMock, patch

from fakeredis import aioredis
import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from shared.contracts.base import BaseMessage
from shared.redis.client import (
    RedisStreamClient,
    StreamMessage,
    TypedMessage,
    decode_redis_value,
)


class SampleMessage(BaseMessage):
    """Minimal BaseMessage subclass for testing."""

    content: str = "test"


class TypedSample(BaseMessage):
    """BaseMessage subclass with a required field, for consume_typed tests."""

    name: str


class SecretSample(BaseMessage):
    """Carries a secret-like field plus a strict field that can fail validation."""

    api_key: str | None = None
    capability: Literal["git", "curl"]


@pytest_asyncio.fixture
async def fake_redis():
    r = aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
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
        # redis-py 8 returns bytes values from XINFO GROUPS even with
        # decode_responses=True — decode before comparing.
        assert any(decode_redis_value(g["name"]) == "test-group" for g in info)

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

    async def test_invalid_json_in_data_field_treated_as_flat(self, client, fake_redis):
        """If 'data' contains invalid JSON, fall back to flat fields."""
        await fake_redis.xadd("s", {"data": "{invalid"})
        received = []
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is not None:
                received.append(msg)
                continue
            break
        assert len(received) == 1
        assert received[0].data == {"data": "{invalid"}


class TestParseFieldsDecoding:
    """redis-py 8 stopped applying decode_responses=True to XREADGROUP/XREAD
    field maps (keys and values arrive as bytes). _parse_fields must normalize
    them to str so consumers keep getting str-keyed dicts on any redis-py.
    """

    def test_flat_bytes_fields_decoded(self):
        data = RedisStreamClient._parse_fields({b"type": b"reminder", b"user_id": b"u1"})
        assert data == {"type": "reminder", "user_id": "u1"}

    def test_wrapped_bytes_data_decoded_and_parsed(self):
        raw = {b"data": b'{"event": "completed", "task_id": "t1"}'}
        assert RedisStreamClient._parse_fields(raw) == {"event": "completed", "task_id": "t1"}

    def test_str_fields_pass_through(self):
        assert RedisStreamClient._parse_fields({"type": "reminder"}) == {"type": "reminder"}


class TestAck:
    async def test_ack_removes_from_pending(self, client, fake_redis):
        """ack() should xack a message, removing it from the PEL."""
        await client.publish("s", {"key": "val"})
        msg_id = None
        async for msg in client.consume("s", "g", "c1", block_ms=100, auto_ack=False):
            if msg is not None:
                msg_id = msg.message_id
                break
        # Message should be pending (not acked)
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 1
        # Manual ack
        await client.ack("s", "g", msg_id)
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0

    async def test_ack_idempotent(self, client, fake_redis):
        """Calling ack() twice on the same message should not raise."""
        await client.publish("s", {"key": "val"})
        async for msg in client.consume("s", "g", "c1", block_ms=100, auto_ack=False):
            if msg is not None:
                await client.ack("s", "g", msg.message_id)
                await client.ack("s", "g", msg.message_id)
                break


class TestConsumeManualAck:
    async def test_auto_ack_false_leaves_pending(self, client, fake_redis):
        """With auto_ack=False, messages stay in PEL after yield."""
        await client.publish("s", {"key": "val"})
        async for msg in client.consume("s", "g", "c1", block_ms=100, auto_ack=False):
            if msg is not None:
                break
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 1

    async def test_manual_ack_after_processing(self, client, fake_redis):
        """Manual ack after consume with auto_ack=False works correctly."""
        await client.publish("s", {"key": "val"})
        async for msg in client.consume("s", "g", "c1", block_ms=100, auto_ack=False):
            if msg is not None:
                # Simulate processing
                await client.ack("s", "g", msg.message_id)
                break
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0

    async def test_auto_ack_true_still_works(self, client, fake_redis):
        """Default auto_ack=True still auto-acks (backwards compatible)."""
        await client.publish("s", {"key": "val"})
        async for msg in client.consume("s", "g", "c1", block_ms=100, auto_ack=True):
            if msg is not None:
                continue  # resume generator so xack runs
            break
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0


class TestConsumePELRecovery:
    async def test_claims_pending_messages(self, client, fake_redis):
        """claim_pending=True should recover messages left in PEL by crashed consumer."""
        # Simulate a crashed consumer: read but never ack
        await fake_redis.xadd("s", {"data": json.dumps({"key": "recovered"})})
        await fake_redis.xgroup_create("s", "g", id="0", mkstream=True)
        await fake_redis.xreadgroup("g", "crashed-consumer", {"s": ">"}, count=1)
        # Verify message is pending
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 1
        # New consumer with claim_pending=True should recover it
        recovered = []
        async for msg in client.consume(
            "s",
            "g",
            "new-consumer",
            block_ms=100,
            auto_ack=False,
            claim_pending=True,
            pending_timeout_ms=0,
        ):
            if msg is not None:
                recovered.append(msg)
                await client.ack("s", "g", msg.message_id)
            else:
                break
        assert len(recovered) == 1
        assert recovered[0].data == {"key": "recovered"}

    async def test_claim_pending_false_skips_recovery(self, client, fake_redis):
        """claim_pending=False should not recover pending messages."""
        await fake_redis.xadd("s", {"data": json.dumps({"key": "lost"})})
        await fake_redis.xgroup_create("s", "g", id="0", mkstream=True)
        await fake_redis.xreadgroup("g", "crashed-consumer", {"s": ">"}, count=1)
        # New consumer with claim_pending=False should NOT see the pending message
        recovered = []
        async for msg in client.consume(
            "s",
            "g",
            "new-consumer",
            block_ms=100,
            auto_ack=False,
            claim_pending=False,
        ):
            if msg is not None:
                recovered.append(msg)
            else:
                break
        assert len(recovered) == 0

    async def test_pel_recovery_then_new_messages(self, client, fake_redis):
        """After recovering pending, should continue reading new messages."""
        # Simulate a crashed consumer
        await fake_redis.xadd("s", {"data": json.dumps({"key": "old"})})
        await fake_redis.xgroup_create("s", "g", id="0", mkstream=True)
        await fake_redis.xreadgroup("g", "crashed", {"s": ">"}, count=1)
        # Add a new message
        await fake_redis.xadd("s", {"data": json.dumps({"key": "new"})})
        # Recover + read new
        all_msgs = []
        async for msg in client.consume(
            "s",
            "g",
            "fresh",
            block_ms=100,
            auto_ack=False,
            claim_pending=True,
            pending_timeout_ms=0,
        ):
            if msg is not None:
                all_msgs.append(msg.data["key"])
                await client.ack("s", "g", msg.message_id)
            else:
                break
        assert "old" in all_msgs
        assert "new" in all_msgs


class TestConsumeFlatFields:
    async def test_flat_fields_without_data_wrapper(self, client, fake_redis):
        """Messages published as flat fields (no 'data' key) should be parsed correctly."""
        await fake_redis.xadd("s", {"type": "user_message", "user_id": "42", "text": "hello"})
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is not None:
                assert msg.data == {"type": "user_message", "user_id": "42", "text": "hello"}
                break

    async def test_data_wrapper_still_works(self, client, fake_redis):
        """Messages published with 'data' JSON wrapper should still work."""
        await client.publish("s", {"key": "val"})
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is not None:
                assert msg.data == {"key": "val"}
                break

    async def test_data_field_that_is_not_json(self, client, fake_redis):
        """If 'data' field exists but is not valid JSON, treat all fields as data."""
        await fake_redis.xadd("s", {"data": "plain-text", "other": "field"})
        async for msg in client.consume("s", "g", "c1", block_ms=100):
            if msg is not None:
                assert msg.data == {"data": "plain-text", "other": "field"}
                break


async def _drain_typed(client, message_type, **kwargs):
    """Consume typed messages until the stream goes idle (first None)."""
    received = []
    async for msg in client.consume_typed("s", "g", "c1", message_type, block_ms=100, **kwargs):
        if msg is None:
            break
        received.append(msg)
    return received


class TestConsumeTyped:
    async def test_valid_message_yields_validated_model(self, client):
        await client.publish("s", {"name": "hello"})
        received = await _drain_typed(client, TypedSample)
        assert len(received) == 1
        assert isinstance(received[0], TypedMessage)
        assert isinstance(received[0].value, TypedSample)
        assert received[0].value.name == "hello"

    async def test_broken_json_is_terminally_acked(self, client, fake_redis):
        """A malformed 'data' payload is logged and ACKed, never yielded."""
        await fake_redis.xadd("s", {"data": "{not valid json"})
        received = await _drain_typed(client, TypedSample)
        assert received == []
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0  # terminal ACK, no poison loop

    async def test_schema_invalid_payload_is_terminally_acked(self, client, fake_redis):
        """Valid JSON that fails validation is discarded terminally."""
        await client.publish("s", {"wrong_field": "x"})  # missing required 'name'
        received = await _drain_typed(client, TypedSample)
        assert received == []
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0

    async def test_valid_message_left_unacked_stays_pending(self, client, fake_redis):
        """consume_typed never auto-acks: a transient failure keeps the entry
        in the PEL for reclaim (caller only acks after success)."""
        await client.publish("s", {"name": "keep"})
        async for msg in client.consume_typed("s", "g", "c1", TypedSample, block_ms=100):
            if msg is not None:
                break  # simulate handler starting, not yet acked
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 1

    async def test_valid_message_acked_after_processing(self, client, fake_redis):
        await client.publish("s", {"name": "done"})
        async for msg in client.consume_typed("s", "g", "c1", TypedSample, block_ms=100):
            if msg is not None:
                await client.ack("s", "g", msg.message_id)
                break
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0

    async def test_validation_error_does_not_log_raw_payload(self, client):
        """A schema-invalid payload with a secret must not leak it into logs."""
        leaked = "ghp_super_secret_token"
        await client.publish("s", {"api_key": leaked, "capability": "not-a-cap"})
        with capture_logs() as logs:
            await _drain_typed(client, SecretSample)
        assert logs, "validation failure should be logged"
        blob = json.dumps(logs, default=str)
        assert leaked not in blob
        assert "not-a-cap" not in blob  # invalid input value must not leak either
        assert any(entry["event"] == "typed_consume_validation_failed" for entry in logs)

    async def test_decode_error_does_not_log_raw_fields(self, client, fake_redis):
        """A malformed 'data' payload with a secret must not leak it into logs."""
        leaked = "ghp_super_secret_token"
        await fake_redis.xadd("s", {"data": f'{{"api_key": "{leaked}", bad json'})
        with capture_logs() as logs:
            await _drain_typed(client, SecretSample)
        blob = json.dumps(logs, default=str)
        assert leaked not in blob
        assert any(entry["event"] == "typed_consume_decode_failed" for entry in logs)

    async def test_terminal_ack_failure_keeps_consumer_alive(self, client, fake_redis):
        """If XACK of a poison entry fails, the consumer keeps serving valid ones."""
        fake_redis.xack = AsyncMock(side_effect=RuntimeError("redis down"))
        await client.publish("s", {"capability": "bad"})  # schema-invalid → terminal ack
        await client.publish("s", {"capability": "git"})  # valid, delivered after
        received = await _drain_typed(client, SecretSample)
        assert [m.value.capability for m in received] == ["git"]


class TestPublishFlat:
    async def test_publish_flat_writes_fields_directly(self, client, fake_redis):
        """publish_flat() should write fields directly without JSON 'data' wrapper."""
        await client.publish_flat("s", {"type": "test", "user_id": "42"})
        messages = await fake_redis.xrange("s")
        assert len(messages) == 1
        fields = messages[0][1]
        assert fields == {"type": "test", "user_id": "42"}
        assert "data" not in fields or fields["data"] != json.dumps(
            {"type": "test", "user_id": "42"}
        )
