"""Callback event publishing helper for worker streams.

Provides a single function to publish progress/completed/failed events
to callback streams, eliminating the repeated boilerplate across workers.

Every helper here takes an already-resolved Telegram chat id. Consumers get it
from the queue message they are processing, whose producer resolved it; nothing
in this module turns an internal user id into a destination.
"""

from __future__ import annotations

import structlog

from shared.contracts.queues.po import POProactiveMessage, POSystemEvent, to_flat_fields
from shared.queues import PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE
from shared.redis_client import RedisStreamClient

logger = structlog.get_logger(__name__)


async def publish_callback_event(
    redis: RedisStreamClient,
    callback_stream: str | None,
    event_type: str,
    task_id: str,
    message: str,
    *,
    telegram_chat_id: str = "",
    project_id: str = "",
    story_id: str = "",
) -> None:
    """Publish a callback event to the stream if configured.

    Args:
        redis: Redis stream client
        callback_stream: Stream name for callback events (None to skip)
        event_type: Event type — "progress", "completed", or "failed"
        task_id: Task ID for the event
        message: Human-readable event message
        telegram_chat_id: Telegram chat this event is addressed to ("" when the
            work has no user recipient)
        project_id: Project ID to include in the event
        story_id: Story ID to include in the event
    """
    if not callback_stream:
        return
    event = POSystemEvent(
        event=event_type,
        task_id=task_id,
        text=message,
        telegram_chat_id=telegram_chat_id,
        project_id=project_id,
        story_id=story_id,
    )
    await redis.publish_flat(callback_stream, to_flat_fields(event))


async def publish_proactive_message(
    redis: RedisStreamClient,
    telegram_chat_id: str,
    message: str,
    *,
    event: str = "",
    story_id: str = "",
    project_id: str = "",
) -> None:
    """Send a proactive notification to the user via Telegram bot.

    Used when there is no callback_stream (e.g. webhook-triggered deploys).

    Args:
        redis: Redis stream client
        telegram_chat_id: Telegram chat id of the recipient.
        message: Text message to send.
        event: Event name carried for admin alerts on failed delivery.
        story_id: Story identifier carried for admin alerts.
        project_id: Project identifier carried for admin alerts.
    """
    if not telegram_chat_id:
        logger.error(
            "proactive_message_without_recipient",
            po_event=event,
            story_id=story_id,
            project_id=project_id,
        )
        return
    msg = POProactiveMessage(
        text=message,
        telegram_chat_id=telegram_chat_id,
        event=event,
        story_id=story_id,
        project_id=project_id,
    )
    await redis.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(msg))


async def publish_story_event(
    redis: RedisStreamClient,
    *,
    telegram_chat_id: str,
    event: str,
    text: str,
    story_id: str = "",
    project_id: str = "",
) -> None:
    """Send a story-level event to PO via po:input.

    PO will craft a user-friendly message instead of forwarding raw text.
    Use event="story_completed" or event="story_failed".
    """
    if not telegram_chat_id:
        logger.error(
            "story_event_without_recipient",
            po_event=event,
            story_id=story_id,
            project_id=project_id,
        )
        return
    msg = POSystemEvent(
        event=event,
        text=text,
        telegram_chat_id=telegram_chat_id,
        story_id=story_id,
        project_id=project_id,
    )
    await redis.publish_flat(PO_INPUT_QUEUE, to_flat_fields(msg))
