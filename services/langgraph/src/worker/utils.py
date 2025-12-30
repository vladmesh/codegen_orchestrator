"""Utility functions for worker."""

import asyncio
from collections import defaultdict

import structlog

logger = structlog.get_logger()

# In-memory conversation history cache
# Key: thread_id, Value: list of messages (last N messages)
MAX_HISTORY_SIZE = 10
conversation_history: dict[str, list] = defaultdict(list)


async def log_memory_stats() -> None:
    """Log conversation history memory usage statistics."""
    total_messages = sum(len(h) for h in conversation_history.values())
    thread_count = len(conversation_history)
    logger.info(
        "memory_stats",
        thread_count=thread_count,
        total_messages=total_messages,
    )


async def periodic_memory_stats() -> None:
    """Periodically log memory statistics."""
    while True:
        await asyncio.sleep(300)  # Log every 5 minutes
        await log_memory_stats()
