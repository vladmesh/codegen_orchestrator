import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
import os
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError
import structlog

from shared.diagnostics import safe_validation_errors

try:
    import redis.asyncio as redis
except ImportError:
    redis = None  # type: ignore

logger = structlog.get_logger(__name__)


def decode_redis_value(value: Any) -> Any:
    """Normalize a Redis response value to str.

    redis-py 8 stopped applying ``decode_responses=True`` to the field maps and
    entry IDs returned by XREADGROUP / XREAD / XAUTOCLAIM (they arrive as bytes),
    even though XRANGE and most other commands still decode. We normalize at the
    boundary so callers always receive str regardless of the redis-py version.
    """
    return value.decode() if isinstance(value, bytes) else value


def decode_redis_fields(fields: dict) -> dict[str, str]:
    """Decode a stream entry's field map to str keys and values."""
    return {decode_redis_value(k): decode_redis_value(v) for k, v in fields.items()}


@dataclass
class StreamMessage:
    """A message from a Redis Stream."""

    message_id: str
    data: dict[str, Any]

    # Helper to parse known DTOs if needed, but 'data' is raw dict


@dataclass
class TypedMessage[T]:
    """A schema-validated message from a Redis Stream.

    ``value`` is a validated Pydantic model, so consumers never touch the raw
    dict. Decode and validation failures are handled terminally inside
    ``consume_typed`` and never surface as a TypedMessage.
    """

    message_id: str
    value: T


DEFAULT_STREAM_MAXLEN = 1000


class RedisStreamClient:
    """Client for Redis Streams-based message passing."""

    def __init__(self, redis_url: str | None = None, *, stream_maxlen: int = DEFAULT_STREAM_MAXLEN):
        """Initialize Redis client.

        Args:
            redis_url: Redis connection URL. Falls back to REDIS_URL env var.
            stream_maxlen: Approximate max messages per stream (MAXLEN ~). 0 to disable.
        """
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        if not self.redis_url:
            raise RuntimeError(
                "Redis URL not provided. Pass redis_url argument or set REDIS_URL env var."
            )
        self._redis: redis.Redis | None = None
        self._stream_maxlen = stream_maxlen

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._redis is None:
            if redis is None:
                raise ImportError("redis package is not installed.")
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            logger.info("redis_connected")

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            logger.info("redis_connection_closed")

    @property
    def redis(self) -> "redis.Redis":
        """Get Redis client, ensuring connection."""
        if self._redis is None:
            raise RuntimeError("Redis not connected. Call connect() first.")
        return self._redis

    def _xadd_kwargs(self) -> dict[str, Any]:
        """Return maxlen kwargs for xadd if configured."""
        if self._stream_maxlen:
            return {"maxlen": self._stream_maxlen, "approximate": True}
        return {}

    async def publish(self, stream: str, data: dict[str, Any]) -> str:
        """Publish a dict to a Redis Stream (wrapped in JSON 'data' field)."""
        message = {"data": json.dumps(data)}
        message_id = await self.redis.xadd(stream, message, **self._xadd_kwargs())
        logger.debug("message_published", stream=stream, message_id=message_id)
        return message_id

    async def publish_flat(self, stream: str, fields: dict[str, str]) -> str:
        """Publish flat key-value fields directly to a Redis Stream (no JSON wrapping)."""
        message_id = await self.redis.xadd(stream, fields, **self._xadd_kwargs())
        logger.debug("message_published_flat", stream=stream, message_id=message_id)
        return message_id

    async def publish_message(self, stream: str, message: BaseModel) -> str:
        """Publish a Pydantic model to a Redis Stream."""
        data = message.model_dump(mode="json")
        return await self.publish(stream, data)

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        """Acknowledge a message, removing it from the pending entries list (PEL)."""
        await self.redis.xack(stream, group, message_id)
        logger.debug("message_acked", stream=stream, message_id=message_id)

    async def ensure_consumer_group(self, stream: str, group: str) -> None:
        """Ensure a consumer group exists for the stream.

        Uses id="0" to process ALL messages including ones sent before group creation.
        This prevents race conditions where messages are sent before worker starts.
        """
        try:
            await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("consumer_group_created", stream=stream, group=group)
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                logger.debug("consumer_group_exists", stream=stream, group=group)
            else:
                raise

    @staticmethod
    def _parse_fields(fields: dict[str, str]) -> dict[str, Any]:
        """Parse Redis stream message fields into a data dict.

        Handles two formats:
        - Wrapped: {"data": "<JSON string>"} → parsed JSON dict
        - Flat: {"key1": "val1", "key2": "val2"} → fields as-is
        """
        fields = decode_redis_fields(fields)
        if "data" in fields:
            try:
                parsed = json.loads(fields["data"])
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return dict(fields)

    @staticmethod
    def _decode_entry(fields: dict[str, str]) -> Any:
        """Strict decode for the typed consume path.

        Unlike ``_parse_fields`` (which silently falls back to the flat field
        map when the wrapped ``data`` payload is malformed), this raises
        ``json.JSONDecodeError`` so ``consume_typed`` can surface a broken
        payload as a terminal error instead of swallowing it.
        """
        fields = decode_redis_fields(fields)
        if "data" in fields:
            return json.loads(fields["data"])
        return dict(fields)

    async def _iter_entries(
        self,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int,
        count: int,
        claim_pending: bool,
        pending_timeout_ms: int,
    ) -> AsyncIterator[tuple[str, dict[str, str]] | None]:
        """Yield raw ``(message_id, fields)`` entries from a stream.

        Shared read plumbing for ``consume`` and ``consume_typed``: ensures the
        group exists, optionally recovers the PEL via XAUTOCLAIM, then blocks on
        XREADGROUP. Never acks — the caller owns ack semantics. Yields ``None``
        when a blocking read returns empty so callers can cede the event loop.
        """
        await self.ensure_consumer_group(stream, group)

        if claim_pending:
            cursor = "0-0"
            while True:
                result = await self.redis.xautoclaim(
                    stream,
                    group,
                    consumer,
                    min_idle_time=pending_timeout_ms,
                    start_id=cursor,
                    count=count,
                )
                new_cursor = decode_redis_value(result[0])
                claimed = result[1]
                for message_id, fields in claimed:
                    if fields is None:
                        continue
                    yield decode_redis_value(message_id), fields
                if new_cursor == "0-0" or not claimed:
                    break
                cursor = new_cursor

        while True:
            try:
                messages = await self.redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=count,
                    block=block_ms,
                )

                if not messages:
                    # Cede control to the event loop. The XREADGROUP block above
                    # normally suspends, but some backends (e.g. fakeredis in
                    # tests) ignore the block timeout and return immediately —
                    # without this yield the loop would busy-spin and starve
                    # other tasks on the same loop.
                    await asyncio.sleep(0)
                    yield None
                    continue

                for _stream_name, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        yield decode_redis_value(message_id), fields

            except asyncio.CancelledError:
                logger.info("consumer_cancelled", consumer=consumer)
                break
            except Exception as e:
                if "NOGROUP" in str(e):
                    logger.warning("consumer_nogroup_recovering", stream=stream, group=group)
                    await self.ensure_consumer_group(stream, group)
                else:
                    logger.error("consume_error", stream=stream, error=str(e))
                await asyncio.sleep(1)

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 5000,
        count: int = 1,
        auto_ack: bool = True,
        claim_pending: bool = False,
        pending_timeout_ms: int = 60_000,
    ) -> AsyncIterator[StreamMessage]:
        """Consume messages from a Redis Stream using consumer groups.

        Args:
            stream: Stream name to consume from.
            group: Consumer group name.
            consumer: Consumer name within the group.
            block_ms: How long to block waiting for messages (ms).
            count: Number of messages to attempt reading per iteration.
            auto_ack: If True, messages are ACKed immediately after yield.
                      If False, caller must call ack() manually.
            claim_pending: If True, recover pending messages (PEL) before reading new ones.
            pending_timeout_ms: Min idle time (ms) for XAUTOCLAIM to claim pending messages.
        """
        async for entry in self._iter_entries(
            stream, group, consumer, block_ms, count, claim_pending, pending_timeout_ms
        ):
            if entry is None:
                yield None  # type: ignore[misc]
                continue
            message_id, fields = entry
            data = self._parse_fields(fields)
            yield StreamMessage(message_id=message_id, data=data)
            if auto_ack:
                await self.redis.xack(stream, group, message_id)
                logger.debug("message_acked", message_id=message_id)

    async def _terminal_ack(self, stream: str, group: str, message_id: str) -> None:
        """ACK a poison entry, tolerating a failing XACK.

        The terminal ACK for an invalid message runs inside the ``consume_typed``
        generator, outside the consumer's own try/except. If XACK hit a transient
        Redis error and propagated, it would kill the consumer generator and
        silently stop the stream from being consumed. So a failed ACK is logged
        and swallowed: the entry stays in the PEL, gets reclaimed, re-validated
        (fails again) and re-ACKed, while the loop keeps serving valid messages.
        """
        try:
            await self.redis.xack(stream, group, message_id)
        except Exception as e:
            logger.error(
                "typed_consume_terminal_ack_failed",
                stream=stream,
                entry_id=message_id,
                error=str(e),
            )

    async def consume_typed[T](
        self,
        stream: str,
        group: str,
        consumer: str,
        message_type: type[T] | TypeAdapter,
        *,
        block_ms: int = 5000,
        count: int = 1,
        claim_pending: bool = True,
        pending_timeout_ms: int = 60_000,
    ) -> AsyncIterator["TypedMessage[T] | None"]:
        """Consume and validate messages against a Pydantic type.

        Yields ``TypedMessage`` holding a validated model. Never auto-acks: the
        caller acks after successful processing, so a transient handler failure
        leaves the entry in the PEL for reclaim.

        Decode and validation errors are terminal. A message that cannot be JSON
        decoded or fails schema validation can never succeed on retry, so leaving
        it unacked would poison the reclaim loop forever. Instead it is logged
        loudly (the human signal) and ACKed away, and never yielded to the caller.

        Args:
            message_type: A Pydantic model type, a union of models, or a prebuilt
                ``TypeAdapter``. Used to validate each message.
        """
        adapter = (
            message_type if isinstance(message_type, TypeAdapter) else TypeAdapter(message_type)
        )

        async for entry in self._iter_entries(
            stream, group, consumer, block_ms, count, claim_pending, pending_timeout_ms
        ):
            if entry is None:
                yield None
                continue
            message_id, fields = entry

            try:
                data = self._decode_entry(fields)
            except json.JSONDecodeError as e:
                # str(JSONDecodeError) is positional only ("Expecting value:
                # line 1 column 1"), so it carries no payload. Never log the raw
                # fields — the payload may hold secrets (tokens in env_vars, api_key).
                logger.error(
                    "typed_consume_decode_failed",
                    stream=stream,
                    entry_id=message_id,
                    error=str(e),
                )
                await self._terminal_ack(stream, group, message_id)
                continue

            try:
                value = adapter.validate_python(data)
            except ValidationError as e:
                # Log structured errors with input elided. str(e) and the raw
                # data both echo field values, which may include secrets, so
                # they must never reach the logs.
                logger.error(
                    "typed_consume_validation_failed",
                    stream=stream,
                    entry_id=message_id,
                    errors=safe_validation_errors(e),
                )
                await self._terminal_ack(stream, group, message_id)
                continue

            yield TypedMessage(message_id=message_id, value=value)
