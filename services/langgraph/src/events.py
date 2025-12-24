"""Event publishing helpers for orchestrator."""

import os

from shared.redis_client import RedisStreamClient

EVENTS_STREAM = "orchestrator:events"

# Global client (initialized lazily)
_redis_client: RedisStreamClient | None = None


async def get_redis_client() -> RedisStreamClient:
    """Get or create Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = RedisStreamClient(os.getenv("REDIS_URL", "redis://localhost:6379"))
        await _redis_client.connect()
    return _redis_client


async def publish_event(event_type: str, data: dict) -> str:
    """Publish event to Redis Stream.

    Args:
        event_type: Event type (e.g., 'project.created')
        data: Event payload

    Returns:
        Message ID from Redis
    """
    client = await get_redis_client()
    return await client.publish(EVENTS_STREAM, {"type": event_type, **data})
