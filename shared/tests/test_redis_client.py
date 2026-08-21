"""Unit tests for shared.redis.client.RedisStreamClient."""

import asyncio
from dataclasses import dataclass, field
import json
import time
from typing import Literal
from unittest.mock import AsyncMock, patch

from fakeredis import aioredis
from pydantic import ConfigDict
import pytest
import pytest_asyncio
import redis as redis_module
from structlog.testing import capture_logs

from shared.contracts.base import BaseMessage
from shared.redis.client import (
    RedisStreamClient,
    StreamMessage,
    TypedMessage,
    decode_redis_value,
    dlq_stream,
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

    async def test_connect_log_omits_credentialed_url(self):
        sentinel = "redis-secret-sentinel"
        c = RedisStreamClient(redis_url=f"redis://user:{sentinel}@redis.example:6379/0")
        with (
            patch(
                "shared.redis.client.redis.from_url",
                return_value=aioredis.FakeRedis(decode_responses=True),
            ),
            capture_logs() as logs,
        ):
            await c.connect()
        assert sentinel not in str(logs)

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

    async def test_socket_timeout_is_idle_without_error_log(self, client, fake_redis):
        fake_redis.xreadgroup = AsyncMock(side_effect=redis_module.exceptions.TimeoutError())

        with capture_logs() as logs:
            async for msg in client.consume("s", "g", "c1", block_ms=5000):
                assert msg is None
                break

        assert not [entry for entry in logs if entry.get("event") == "consume_error"]

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


class TestLegacyRecipientRejection:
    """A message addressed by the removed ``user_id`` is refused, loudly."""

    async def test_a_legacy_addressed_message_alerts_admins_and_is_not_yielded(
        self, client, fake_redis
    ):
        await client.publish(
            "s",
            {
                "user_id": "1",
                "story_id": "story-7",
                "project_id": "proj-3",
                "event": "story_completed",
            },
        )

        with patch("shared.notifications.notify_admins_best_effort", new=AsyncMock()) as alert:
            with capture_logs() as logs:
                received = await _drain_typed(client, TypedSample)

        assert received == [], "unaddressable work is never handed to the consumer"
        alert.assert_awaited_once()
        text = alert.await_args.args[0]
        assert "user_id" in text
        assert "story-7" in text
        assert "proj-3" in text
        assert any(entry["event"] == "legacy_recipient_field_rejected" for entry in logs)
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 0, "rejected terminally, not left to poison the loop"

    async def test_an_ordinary_invalid_message_raises_no_recipient_alert(self, client):
        await client.publish("s", {"wrong_field": "x"})

        with patch("shared.notifications.notify_admins_best_effort", new=AsyncMock()) as alert:
            assert await _drain_typed(client, TypedSample) == []

        alert.assert_not_awaited()


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


class StrictSample(BaseMessage):
    """A strict contract: unknown fields are refused, as the queue DTOs are."""

    model_config = ConfigDict(extra="forbid")

    name: str


class StrictCreate(BaseMessage):
    """Union member, strict, distinguished by ``command``."""

    model_config = ConfigDict(extra="forbid")

    command: Literal["create"] = "create"
    payload: str


class StrictDelete(BaseMessage):
    """The other union member."""

    model_config = ConfigDict(extra="forbid")

    command: Literal["delete"] = "delete"
    worker_id: str


@dataclass
class _PelEntry:
    """One entry in a consumer group's PEL, as the scan below sees it."""

    entry_id: str
    idle_ms: int
    fields: dict[str, str] = field(default_factory=lambda: {"data": "{}"})


class _RedisPelScan:
    """XAUTOCLAIM with the scan limit real Redis applies and fakeredis does not.

    Redis walks at most about ``COUNT * 10`` PEL entries per call and then
    answers with wherever it stopped, so a call can come back with an advanced
    cursor, no claimed entries and no deleted ids. fakeredis instead filters the
    whole PEL by idle time and returns ``start`` as the cursor whenever it
    claimed nothing, which cannot express that answer. Reproduced against
    ``redis:7.4.10-alpine``: eleven pending entries, the first ten refreshed,
    the eleventh stale, ``COUNT 1`` -> ``cursor=<eleventh-id>, claimed=[],
    deleted=[]``.
    """

    def __init__(self, entries: list[_PelEntry]):
        self.entries = entries
        self.start_ids: list[str] = []

    async def __call__(self, stream, group, consumer, *, min_idle_time, start_id, count):
        self.start_ids.append(start_id)
        cursor = "0-0"
        claimed: list[tuple[str, dict[str, str]]] = []
        scanned = 0
        for entry in (e for e in self.entries if e.entry_id >= start_id):
            if scanned >= count * 10 or len(claimed) >= count:
                cursor = entry.entry_id
                break
            scanned += 1
            if entry.idle_ms >= min_idle_time:
                claimed.append((entry.entry_id, entry.fields))
        return [cursor, claimed, []]


class TestReclaimRunsForTheConsumerLifetime:
    """XAUTOCLAIM used to run once, before the read loop. The generator is built
    once per process, so an entry that got stuck after start-up stayed in the
    PEL until the service was restarted."""

    async def test_entry_stuck_after_startup_is_reclaimed_without_a_restart(
        self, client, fake_redis
    ):
        entries = client.consume(
            "s",
            "g",
            "live-consumer",
            block_ms=10,
            auto_ack=False,
            claim_pending=True,
            pending_timeout_ms=0,
        ).__aiter__()

        # The consumer starts against an empty stream: the start-up sweep finds
        # nothing, and it settles into its read loop.
        assert await anext(entries) is None

        # Only now does another consumer take an entry and die without acking.
        await fake_redis.xadd("s", {"data": json.dumps({"key": "stuck"})})
        await fake_redis.xreadgroup("g", "dead-consumer", {"s": ">"}, count=1)
        assert (await fake_redis.xpending("s", "g"))["pending"] == 1

        # The same, still running generator has to bring it back. Give it more
        # than one reclaim interval to do so.
        received = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            msg = await anext(entries)
            if msg is not None:
                received.append(msg.data)
                break
            await asyncio.sleep(0.02)
        await entries.aclose()

        assert received == [{"key": "stuck"}], "a live consumer never reclaimed the stuck entry"

    async def test_a_stale_tail_behind_a_fresh_prefix_is_still_reclaimed(self, client, fake_redis):
        """A page with nothing to claim must not end the sweep.

        Redis stops an XAUTOCLAIM call after scanning roughly ``count * 10``
        PEL entries. When those entries all belong to a consumer that is still
        healthy, the call answers with an advanced cursor, an empty claim list
        and no deleted ids. The sweep used to treat that empty page as the end
        of the PEL, so a stale entry sitting behind a fresh prefix was never
        reclaimed — and since every sweep restarts at "0-0", it walked into the
        same prefix and stopped again for as long as the prefix stayed fresh.
        """
        fresh = [_PelEntry(f"{1000 + i}-0", idle_ms=0) for i in range(10)]
        stale = _PelEntry("1010-0", idle_ms=60_000, fields={"data": json.dumps({"key": "tail"})})
        pel = _RedisPelScan([*fresh, stale])
        fake_redis.xautoclaim = pel

        received = []
        entries = client.consume(
            "s",
            "g",
            "live-consumer",
            block_ms=10,
            auto_ack=False,
            claim_pending=True,
            pending_timeout_ms=1_000,
            reclaim_interval_ms=10,
        ).__aiter__()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            msg = await anext(entries)
            if msg is not None:
                received.append(msg.data)
                break
            await asyncio.sleep(0.02)
        await entries.aclose()

        assert received == [{"key": "tail"}], (
            "the sweep stopped on the fresh prefix instead of following the cursor"
        )
        assert pel.start_ids[:2] == ["0-0", "1010-0"], (
            "the second call has to resume from the cursor Redis handed back"
        )

    async def test_a_sweep_never_lowers_the_idle_bar_that_protects_a_healthy_consumer(
        self, client, fake_redis
    ):
        """The periodic sweep still passes ``pending_timeout_ms`` as min_idle_time."""
        claims = []
        fake_redis.xautoclaim = AsyncMock(
            side_effect=lambda *a, **kw: claims.append(kw["min_idle_time"]) or ("0-0", [])
        )
        async for _ in client.consume(
            "s", "g", "c1", block_ms=10, claim_pending=True, pending_timeout_ms=45_000
        ):
            break
        assert claims == [45_000]


class TestTrimmedEntryIsDiagnosed:
    """A PEL entry whose body the stream no longer holds — trimmed by MAXLEN on
    publish or by the scheduler's XTRIM MINID — used to be skipped in silence."""

    async def test_body_less_claim_is_logged_and_counted(self, client, fake_redis):
        # Redis 6.2 shape: the claimed list carries the entry with no fields.
        fake_redis.xautoclaim = AsyncMock(return_value=("0-0", [("5-5", None)]))

        with capture_logs() as logs:
            async for _ in client.consume(
                "s", "g", "c1", block_ms=10, claim_pending=True, pending_timeout_ms=0
            ):
                break

        lost = [entry for entry in logs if entry["event"] == "stream_entry_lost_to_trim"]
        assert lost, "an entry lost to a trim left no trace"
        assert lost[0]["entry_id"] == "5-5"
        assert lost[0]["stream"] == "s"
        assert await client.lost_entry_count("s", "g") == 1

    async def test_deleted_ids_reported_by_redis_7_are_counted_too(self, client, fake_redis):
        # Redis 7 shape: dropped ids come back in a third element instead.
        fake_redis.xautoclaim = AsyncMock(return_value=("0-0", [], ["9-9"]))

        with capture_logs() as logs:
            async for _ in client.consume(
                "s", "g", "c1", block_ms=10, claim_pending=True, pending_timeout_ms=0
            ):
                break

        assert [e["entry_id"] for e in logs if e["event"] == "stream_entry_lost_to_trim"] == ["9-9"]
        assert await client.lost_entry_count("s", "g") == 1

    async def test_counter_accumulates_across_streams_separately(self, client, fake_redis):
        fake_redis.xautoclaim = AsyncMock(return_value=("0-0", [("5-5", None)]))
        for _ in range(2):
            async for _ in client.consume(
                "s", "g", "c1", block_ms=10, claim_pending=True, pending_timeout_ms=0
            ):
                break
        assert await client.lost_entry_count("s", "g") == 2
        assert await client.lost_entry_count("other", "g") == 0


class TestPoisonEntryIsQuarantined:
    """A poison entry is ACKed away — but only after it lands somewhere a human
    can read it back from."""

    async def test_undecodable_body_is_recoverable_from_the_dlq(self, client, fake_redis):
        raw = '{"api_key": "ghp_super_secret_token", bad json'
        await fake_redis.xadd("s", {"data": raw})

        assert await _drain_typed(client, TypedSample) == []

        quarantined = await fake_redis.xrange(dlq_stream("s"))
        assert len(quarantined) == 1, "poison entry vanished instead of being quarantined"
        entry_id, fields = quarantined[0]
        assert fields["source_stream"] == "s"
        assert fields["group"] == "g"
        assert fields["failure"] == "decode_error"
        assert json.loads(fields["body"]) == {"data": raw}
        assert (await fake_redis.xpending("s", "g"))["pending"] == 0

    async def test_schema_invalid_body_is_recoverable_with_an_elided_reason(
        self, client, fake_redis
    ):
        await client.publish("s", {"api_key": "ghp_super_secret_token", "capability": "not-a-cap"})

        assert await _drain_typed(client, SecretSample) == []

        quarantined = await fake_redis.xrange(dlq_stream("s"))
        assert len(quarantined) == 1
        fields = quarantined[0][1]
        assert fields["failure"] == "validation_error"
        reason = json.loads(fields["reason"])
        assert reason == [{"type": "literal_error", "loc": ["capability"]}]
        assert "not-a-cap" not in fields["reason"], "the reason must not echo the payload"
        # The body is kept whole, secrets and all: the DLQ is a stream in the
        # same Redis the payload already sat in, unlike a log.
        assert json.loads(json.loads(fields["body"])["data"])["api_key"] == (
            "ghp_super_secret_token"
        )

    async def test_the_quarantine_copy_is_never_logged(self, client, fake_redis):
        leaked = "ghp_super_secret_token"
        await client.publish("s", {"api_key": leaked, "capability": "not-a-cap"})
        with capture_logs() as logs:
            await _drain_typed(client, SecretSample)
        assert leaked not in json.dumps(logs, default=str)
        assert any(entry["event"] == "typed_consume_entry_quarantined" for entry in logs)

    async def test_an_entry_that_cannot_be_quarantined_is_not_acked_away(self, client, fake_redis):
        real_xadd = fake_redis.xadd

        async def refuse_dlq(name, *args, **kwargs):
            if name.endswith(":dlq"):
                raise RuntimeError("redis refused the write")
            return await real_xadd(name, *args, **kwargs)

        fake_redis.xadd = refuse_dlq
        await client.publish("s", {"wrong_field": "x"})

        with capture_logs() as logs:
            assert await _drain_typed(client, TypedSample) == []

        assert any(entry["event"] == "typed_consume_quarantine_failed" for entry in logs)
        pending = await fake_redis.xpending("s", "g")
        assert pending["pending"] == 1, "destroyed the message because its DLQ copy failed"

    async def test_a_legacy_addressed_message_is_quarantined_as_well(self, client, fake_redis):
        await client.publish("s", {"user_id": "1", "event": "story_completed"})
        with patch("shared.notifications.notify_admins_best_effort", new=AsyncMock()):
            assert await _drain_typed(client, TypedSample) == []
        assert len(await fake_redis.xrange(dlq_stream("s"))) == 1


class TestNewerPublisherIsNotDestructive:
    """Publisher and consumer are built into separate images, so one of them is
    always the older one for a while."""

    async def test_extra_field_from_a_newer_publisher_is_still_delivered(self, client, fake_redis):
        await client.publish("s", {"name": "hello", "cost_usd": 0.42})

        received = await _drain_typed(client, StrictSample)

        assert [m.value.name for m in received] == ["hello"]
        assert await fake_redis.xlen(dlq_stream("s")) == 0

    async def test_the_ignored_field_is_named_in_the_log_without_its_value(self, client):
        await client.publish("s", {"name": "hello", "api_key": "ghp_super_secret_token"})
        with capture_logs() as logs:
            await _drain_typed(client, StrictSample)
        ignored = [e for e in logs if e["event"] == "typed_consume_unknown_fields_ignored"]
        assert ignored and ignored[0]["unknown_fields"] == ["api_key"]
        assert "ghp_super_secret_token" not in json.dumps(logs, default=str)

    async def test_a_newer_field_on_a_union_member_is_delivered_to_that_member(self, client):
        await client.publish("s", {"command": "create", "payload": "x", "cost_usd": 0.42})

        received = await _drain_typed(client, StrictCreate | StrictDelete)

        assert len(received) == 1
        assert isinstance(received[0].value, StrictCreate)
        assert received[0].value.payload == "x"

    async def test_a_payload_that_is_wrong_rather_than_new_still_goes_to_the_dlq(
        self, client, fake_redis
    ):
        # Missing the required field *and* carrying an unknown one: not a newer
        # publisher, so nothing is forgiven.
        await client.publish("s", {"cost_usd": 0.42})

        assert await _drain_typed(client, StrictSample) == []
        assert await fake_redis.xlen(dlq_stream("s")) == 1

    async def test_an_unknown_queue_version_is_quarantined_not_accepted(self, client, fake_redis):
        await client.publish("s", {"name": "hello", "version": "2"})

        assert await _drain_typed(client, StrictSample) == []
        quarantined = await fake_redis.xrange(dlq_stream("s"))
        assert len(quarantined) == 1
        assert json.loads(quarantined[0][1]["reason"]) == [
            {"type": "literal_error", "loc": ["version"]}
        ]

    def test_publishing_an_unknown_field_still_fails_at_the_publisher(self):
        """Tolerance is read-side only: the write side keeps ``extra="forbid"``."""
        with pytest.raises(ValueError, match="cost_usd"):
            StrictSample(name="hello", cost_usd=0.42)
