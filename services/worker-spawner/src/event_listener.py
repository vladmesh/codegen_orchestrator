"""Worker event listener for coding worker results."""

from __future__ import annotations

import json

from pydantic import ValidationError
from redis.asyncio.client import PubSub
import structlog

from shared.schemas.worker_events import WorkerEventUnion, parse_worker_event

logger = structlog.get_logger(__name__)

TERMINAL_EVENT_TYPES = {"completed", "failed"}


async def wait_for_terminal_event(pubsub: PubSub) -> WorkerEventUnion | None:
    """Listen for worker events until a terminal event arrives."""

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue

        payload = message.get("data")
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("worker_event_json_invalid", payload=payload)
            continue

        try:
            event = parse_worker_event(data)
        except ValidationError as exc:
            logger.warning("worker_event_validation_failed", errors=exc.errors())
            continue

        logger.info(
            "worker_event_received",
            event_type=event.event_type,
            request_id=event.request_id,
        )

        if event.event_type in TERMINAL_EVENT_TYPES:
            return event

    return None
