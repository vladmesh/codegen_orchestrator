"""Redis Streams client for inter-service communication.

Redis URL is passed explicitly or read from environment.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
import os
from typing import Any

import redis.asyncio as redis
import structlog

logger = structlog.get_logger(__name__)


@dataclass
class StreamMessage:
    """A message from a Redis Stream."""

    message_id: str
    data: dict[str, Any]


class RedisStreamClient:
    """Client for Redis Streams-based message passing."""

    # Stream names
    INCOMING_STREAM = "telegram:incoming"
    OUTGOING_STREAM = "telegram:outgoing"

    def __init__(self, redis_url: str | None = None):
        """Initialize Redis client.

        Args:
            redis_url: Redis connection URL. Falls back to REDIS_URL env var.
                      Raises RuntimeError if neither is provided.
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
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
            logger.info("redis_connected", redis_url=self.redis_url)

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.aclose()
            self._redis = None
            logger.info("redis_connection_closed")

    @property
    def redis(self) -> redis.Redis:
        """Get Redis client, ensuring connection."""
        if self._redis is None:
            raise RuntimeError("Redis not connected. Call connect() first.")
        return self._redis

    async def publish(self, stream: str, data: dict[str, Any]) -> str:
        """Publish a message to a Redis Stream.

        Args:
            stream: Stream name to publish to.
            data: Message data (will be JSON serialized).

        Returns:
            Message ID assigned by Redis.
        """
        # Serialize data to JSON string for Redis
        message = {"data": json.dumps(data)}
        message_id = await self.redis.xadd(stream, message)
        logger.debug("message_published", stream=stream, message_id=message_id)
        return message_id

    async def ensure_consumer_group(self, stream: str, group: str) -> None:
        """Ensure a consumer group exists for the stream.

        Creates the stream and group if they don't exist.
        """
        try:
            await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            logger.info("consumer_group_created", stream=stream, group=group)
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                # Group already exists, that's fine
                logger.debug("consumer_group_exists", stream=stream, group=group)
            else:
                raise

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 5000,
    ) -> AsyncIterator[StreamMessage]:
        """Consume messages from a Redis Stream using consumer groups.

        Args:
            stream: Stream name to consume from.
            group: Consumer group name.
            consumer: Consumer name within the group.
            block_ms: How long to block waiting for messages (milliseconds).

        Yields:
            StreamMessage objects with message_id and parsed data.
        """
        await self.ensure_consumer_group(stream, group)

        while True:
            try:
                # Read from consumer group
                # ">" means only new messages not yet delivered to any consumer
                messages = await self.redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=1,
                    block=block_ms,
                )

                if not messages:
                    continue

                for _stream_name, stream_messages in messages:
                    for message_id, fields in stream_messages:
                        try:
                            data = json.loads(fields.get("data", "{}"))
                            yield StreamMessage(message_id=message_id, data=data)

                            # Acknowledge the message
                            await self.redis.xack(stream, group, message_id)
                            logger.debug("message_acked", message_id=message_id)

                        except json.JSONDecodeError as e:
                            logger.error(
                                "message_parse_failed", message_id=message_id, error=str(e)
                            )
                            # Still ACK to avoid reprocessing bad messages
                            await self.redis.xack(stream, group, message_id)

            except asyncio.CancelledError:
                logger.info("consumer_cancelled", consumer=consumer)
                break
            except Exception as e:
                logger.error("consume_error", stream=stream, error=str(e))
                await asyncio.sleep(1)  # Brief pause before retry
