"""Callback event publishing helper for worker streams.

Provides a single function to publish progress/completed/failed events
to callback streams, eliminating the repeated boilerplate across workers.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json

from shared.redis_client import RedisStreamClient


async def publish_callback_event(
    redis: RedisStreamClient,
    callback_stream: str | None,
    event_type: str,
    task_id: str,
    message: str,
) -> None:
    """Publish a callback event to the stream if configured.

    Args:
        redis: Redis stream client
        callback_stream: Stream name for callback events (None to skip)
        event_type: Event type — "progress", "completed", or "failed"
        task_id: Task ID for the event
        message: Human-readable event message
    """
    if not callback_stream:
        return
    await redis.redis.xadd(
        callback_stream,
        {
            "data": json.dumps(
                {
                    "type": event_type,
                    "task_id": task_id,
                    "message": message,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            )
        },
    )
