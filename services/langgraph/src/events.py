"""Event publishing helpers for orchestrator.

Uses service-specific config for Redis URL.
"""

from shared.redis_client import RedisStreamClient
from src.config.settings import get_settings

EVENTS_STREAM = "orchestrator:events"

# Global client (initialized lazily)
_redis_client: RedisStreamClient | None = None


async def get_redis_client() -> RedisStreamClient:
    """Get or create Redis client."""
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = RedisStreamClient(settings.redis_url)
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
