import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
import os
from typing import Any

import structlog

try:
    import redis.asyncio as redis
except ImportError:
    redis = None  # type: ignore

from shared.contracts.base import BaseMessage

logger = structlog.get_logger(__name__)


@dataclass
class StreamMessage:
    """A message from a Redis Stream."""

    message_id: str
    data: dict[str, Any]

    # Helper to parse known DTOs if needed, but 'data' is raw dict


class RedisStreamClient:
    """Client for Redis Streams-based message passing."""

    def __init__(self, redis_url: str | None = None):
        """Initialize Redis client.

        Args:
            redis_url: Redis connection URL. Falls back to REDIS_URL env var.
        """
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        if not self.redis_url:
            raise RuntimeError(
                "Redis URL not provided. Pass redis_url argument or set REDIS_URL env var."
            )
        self._redis: redis.Redis | None = None

    async def connect(self) -> None:
        """Connect to Redis."""
        if self._redis is None:
            if redis is None:
                raise ImportError("redis package is not installed.")
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            logger.info("redis_connected", redis_url=self.redis_url)

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

    async def publish(self, stream: str, data: dict[str, Any]) -> str:
        """Publish a dict to a Redis Stream (wrapped in JSON 'data' field)."""
        message = {"data": json.dumps(data)}
        message_id = await self.redis.xadd(stream, message)
        logger.debug("message_published", stream=stream, message_id=message_id)
        return message_id

    async def publish_flat(self, stream: str, fields: dict[str, str]) -> str:
        """Publish flat key-value fields directly to a Redis Stream (no JSON wrapping)."""
        message_id = await self.redis.xadd(stream, fields)
        logger.debug("message_published_flat", stream=stream, message_id=message_id)
        return message_id

    async def publish_message(self, stream: str, message: BaseMessage) -> str:
        """Publish a Pydantic DTO to a Redis Stream."""
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
        if "data" in fields:
            try:
                parsed = json.loads(fields["data"])
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return dict(fields)

    async def _recover_pending(
        self,
        stream: str,
        group: str,
        consumer: str,
        pending_timeout_ms: int,
        count: int,
        auto_ack: bool,
    ) -> AsyncIterator[StreamMessage]:
        """Recover pending messages via XAUTOCLAIM before reading new ones."""
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
            new_cursor = result[0]
            claimed = result[1]
            for message_id, fields in claimed:
                if fields is None:
                    continue
                data = self._parse_fields(fields)
                yield StreamMessage(message_id=message_id, data=data)
                if auto_ack:
                    await self.redis.xack(stream, group, message_id)
                    logger.debug("message_acked", message_id=message_id)
            if new_cursor == "0-0" or not claimed:
                break
            cursor = new_cursor

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
        await self.ensure_consumer_group(stream, group)

        if claim_pending:
            async for msg in self._recover_pending(
                stream, group, consumer, pending_timeout_ms, count, auto_ack
            ):
                yield msg

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
                    yield None  # type: ignore[misc]
                    continue

                for _stream_name, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        data = self._parse_fields(fields)
                        yield StreamMessage(message_id=message_id, data=data)
                        if auto_ack:
                            await self.redis.xack(stream, group, message_id)
                            logger.debug("message_acked", message_id=message_id)

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
