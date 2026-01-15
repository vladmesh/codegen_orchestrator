"""Redis publisher for LangGraph service.

Publishes messages to Redis Streams for communication with other services.
"""

import redis.asyncio as redis
import structlog

from ..config.settings import get_settings

logger = structlog.get_logger()


class RedisPublisher:
    """Publishes messages to Redis Streams."""

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or get_settings().redis_url
        self._client: redis.Redis | None = None

    async def get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._client is None:
            self._client = redis.from_url(self.redis_url, decode_responses=False)
        return self._client

    async def publish(self, stream: str, data: str) -> str:
        """Publish message to a Redis Stream.

        Args:
            stream: Stream name (e.g., "scaffolder:queue")
            data: JSON string payload

        Returns:
            Message ID from Redis
        """
        client = await self.get_client()
        msg_id = await client.xadd(stream, {"data": data})

        logger.debug(
            "message_published",
            stream=stream,
            msg_id=msg_id,
        )

        return msg_id

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None


# Singleton instance
_publisher: RedisPublisher | None = None


def get_publisher() -> RedisPublisher:
    """Get singleton publisher instance."""
    global _publisher
    if _publisher is None:
        _publisher = RedisPublisher()
    return _publisher
