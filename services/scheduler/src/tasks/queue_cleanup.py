"""Periodic cleanup of orphan Redis streams and old queue messages.

Cleans:
1. Orphan po:response:* streams (created for PO request-response, not deleted on timeout)
2. Orphan worker:*:input and worker:*:output streams (left by deleted workers)
3. Old messages in task queues via XTRIM MINID (safety net beyond MAXLEN)
"""

from __future__ import annotations

import asyncio
import time

import structlog

from shared.queues import JOB_TTL_SECONDS, QUEUE_TOPOLOGY
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)

# Patterns for ephemeral streams that should be cleaned when idle
_ORPHAN_PATTERNS = [
    "po:response:*",
    "worker:*:input",
    "worker:*:output",
]

# Default idle threshold: 10 minutes (PO response timeout is 5 min)
DEFAULT_IDLE_THRESHOLD_S = 600

# Cleanup interval: 10 minutes
CLEANUP_INTERVAL_S = 600


async def _scan_keys(redis, pattern: str) -> list[str]:
    """Collect all keys matching pattern via SCAN (non-blocking iteration)."""
    keys: list[str] = []
    cursor = 0
    while True:
        cursor, batch = await redis.scan(cursor, match=pattern, count=100)
        keys.extend(batch)
        if cursor == 0:
            break
    return keys


async def _clean_orphan_streams(
    client: RedisStreamClient,
    idle_threshold_s: int = DEFAULT_IDLE_THRESHOLD_S,
) -> int:
    """Delete ephemeral streams that have been idle beyond threshold.

    Returns number of streams deleted.
    """
    redis = client.redis
    cleaned = 0

    for pattern in _ORPHAN_PATTERNS:
        keys = await _scan_keys(redis, pattern)
        for key in keys:
            try:
                idle_s = await redis.object("idletime", key)
                if idle_s >= idle_threshold_s:
                    await redis.delete(key)
                    cleaned += 1
                    logger.debug("orphan_stream_deleted", key=key, idle_s=idle_s)
            except Exception:
                logger.debug("orphan_stream_check_failed", key=key, exc_info=True)

    if cleaned:
        logger.info("orphan_streams_cleaned", count=cleaned)

    return cleaned


async def _trim_old_messages(
    client: RedisStreamClient,
    ttl_seconds: int = JOB_TTL_SECONDS,
) -> int:
    """Trim messages older than TTL from all task queues using XTRIM MINID.

    Returns total number of entries trimmed across all queues.
    """
    redis = client.redis
    cutoff_ms = int((time.time() - ttl_seconds) * 1000)
    minid = f"{cutoff_ms}-0"
    total_trimmed = 0

    for binding in QUEUE_TOPOLOGY:
        try:
            trimmed = await redis.xtrim(binding.stream, minid=minid)
            if trimmed:
                logger.info(
                    "queue_trimmed",
                    stream=binding.stream,
                    trimmed=trimmed,
                    minid=minid,
                )
                total_trimmed += trimmed
        except Exception:
            logger.warning(
                "queue_trim_failed",
                stream=binding.stream,
                exc_info=True,
            )

    return total_trimmed


async def queue_cleanup_worker() -> None:
    """Periodically clean orphan streams and trim old queue messages.

    Runs every CLEANUP_INTERVAL_S (10 minutes).
    """
    client = RedisStreamClient()
    await client.connect()

    logger.info("queue_cleanup_worker_started", interval_s=CLEANUP_INTERVAL_S)

    try:
        while True:
            try:
                orphans = await _clean_orphan_streams(client)
                trimmed = await _trim_old_messages(client)
                logger.debug(
                    "queue_cleanup_cycle_done",
                    orphans_cleaned=orphans,
                    messages_trimmed=trimmed,
                )
            except Exception:
                logger.error("queue_cleanup_error", exc_info=True)

            await asyncio.sleep(CLEANUP_INTERVAL_S)
    except asyncio.CancelledError:
        logger.info("queue_cleanup_worker_cancelled")
    finally:
        await client.close()
        logger.info("queue_cleanup_worker_stopped")
