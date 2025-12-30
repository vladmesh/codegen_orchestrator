"""Worker event forwarding to orchestrator stream."""

import asyncio
import json

from pydantic import ValidationError
import redis.asyncio as redis
import structlog

from shared.schemas.worker_events import parse_worker_event

from ..config.settings import get_settings
from ..events import publish_event

logger = structlog.get_logger()

WORKER_EVENTS_ALL_CHANNEL = "worker:events:all"


async def listen_worker_events() -> None:
    """Listen for worker progress events and forward them to orchestrator stream."""
    settings = get_settings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()

    await pubsub.subscribe(WORKER_EVENTS_ALL_CHANNEL)
    logger.info("worker_events_subscribed", channel=WORKER_EVENTS_ALL_CHANNEL)

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            try:
                data = json.loads(message["data"])
            except json.JSONDecodeError:
                logger.warning("worker_event_invalid_json")
                continue

            try:
                event = parse_worker_event(data)
            except ValidationError as exc:
                logger.warning("worker_event_validation_failed", errors=exc.errors())
                continue

            try:
                await publish_event(f"worker.{event.event_type}", event.model_dump())
            except Exception as exc:
                logger.warning(
                    "worker_event_forward_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
    except asyncio.CancelledError:
        logger.info("worker_events_listener_cancelled")
    except Exception as e:
        logger.error(
            "worker_events_listener_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
    finally:
        await client.close()
