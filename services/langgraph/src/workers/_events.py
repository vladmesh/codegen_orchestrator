"""Callback event publishing helper for worker streams.

Provides a single function to publish progress/completed/failed events
to callback streams, eliminating the repeated boilerplate across workers.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.redis_client import RedisStreamClient


async def publish_callback_event(
    redis: RedisStreamClient,
    callback_stream: str | None,
    event_type: str,
    task_id: str,
    message: str,
    *,
    user_id: str = "",
    project_id: str = "",
) -> None:
    """Publish a callback event to the stream if configured.

    Args:
        redis: Redis stream client
        callback_stream: Stream name for callback events (None to skip)
        event_type: Event type — "progress", "completed", or "failed"
        task_id: Task ID for the event
        message: Human-readable event message
        user_id: User ID to include in the event
        project_id: Project ID to include in the event
    """
    if not callback_stream:
        return
    fields = {
        "type": "system_event",
        "event": event_type,
        "task_id": task_id,
        "text": message,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if user_id:
        fields["user_id"] = user_id
    if project_id:
        fields["project_id"] = project_id
    await redis.redis.xadd(callback_stream, fields)
