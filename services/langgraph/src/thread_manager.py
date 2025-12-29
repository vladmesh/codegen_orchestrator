"""Thread ID management for Dynamic PO sessions.

Thread IDs are used for:
- LangGraph checkpointing (conversation state)
- RAG context scoping
- Session tracking and logging

Format: user_{user_id}_{sequence}
Example: user_625038902_43

Redis key: thread:sequence:{user_id} → integer sequence
"""

import os

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()

# Redis client singleton
_redis_client: redis.Redis | None = None


async def _get_redis() -> redis.Redis:
    """Get or create Redis client for thread management."""
    global _redis_client
    if _redis_client is None:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _redis_client = redis.from_url(redis_url, decode_responses=True)
    return _redis_client


async def generate_thread_id(user_id: int) -> str:
    """Increment sequence and return new thread_id.

    Called by intent_parser for new tasks.

    Args:
        user_id: Telegram user ID

    Returns:
        Thread ID in format "user_{user_id}_{sequence}"
        Example: "user_625038902_43"
    """
    r = await _get_redis()
    sequence = await r.incr(f"thread:sequence:{user_id}")
    thread_id = f"user_{user_id}_{sequence}"

    logger.debug("thread_id_generated", user_id=user_id, sequence=sequence, thread_id=thread_id)
    return thread_id


async def get_current_thread_id(user_id: int) -> str | None:
    """Get current thread_id without incrementing.

    Args:
        user_id: Telegram user ID

    Returns:
        Current thread_id or None if user has no threads yet.
    """
    r = await _get_redis()
    sequence = await r.get(f"thread:sequence:{user_id}")
    if sequence is None:
        return None
    return f"user_{user_id}_{int(sequence)}"


async def get_or_create_thread_id(user_id: int) -> str:
    """Get current thread_id or create first one.

    Used when continuing existing session.

    Args:
        user_id: Telegram user ID

    Returns:
        Existing thread_id or new one if first interaction.
    """
    current = await get_current_thread_id(user_id)
    if current is None:
        return await generate_thread_id(user_id)
    return current
