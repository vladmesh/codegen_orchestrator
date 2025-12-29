"""LangGraph worker - consumes messages from Redis and processes through graph."""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
import json
import sys
import time

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import ValidationError
import redis.asyncio as redis
import structlog

from shared.logging_config import setup_logging
from shared.schemas.worker_events import parse_worker_event

# Add shared to path
sys.path.insert(0, "/app")
from shared.redis_client import RedisStreamClient

from .clients.api import api_client
from .config.settings import get_settings
from .events import publish_event
from .graph import OrchestratorState, create_graph
from .thread_manager import get_or_create_thread_id

logger = structlog.get_logger()

# In-memory conversation history cache
# Key: thread_id, Value: list of messages (last N messages)
MAX_HISTORY_SIZE = 10
conversation_history: dict[str, list] = defaultdict(list)

# Avoid duplicate provisioning runs for the same server.
PROVISIONING_TRIGGER_COOLDOWN_SECONDS = 120
active_provisioning: set[str] = set()
provisioning_cooldowns: dict[str, datetime] = {}

# Create graph once at startup (with MemorySaver)
graph = create_graph()


async def _resolve_user_id(telegram_id: int) -> int | None:
    """Resolve internal user.id from telegram_id via API.

    Returns None if user not found or API error.
    """
    try:
        user_data = await api_client.get_user_by_telegram(telegram_id)
        return user_data.get("id")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.NOT_FOUND:
            logger.debug("user_not_found_in_db", telegram_id=telegram_id)
        else:
            logger.warning(
                "user_resolution_unexpected_status",
                telegram_id=telegram_id,
                status_code=exc.response.status_code,
            )
    except Exception as exc:
        logger.warning(
            "user_id_resolution_failed",
            telegram_id=telegram_id,
            error=str(exc),
        )
    return None


async def _get_conversation_context(user_id: int) -> str | None:
    """Fetch recent conversation summaries for context enrichment."""
    try:
        # api_client.get() already returns parsed JSON (list or dict)
        summaries = await api_client.get(f"rag/summaries?user_id={user_id}&limit=3")
        if summaries:
            return "\n\n".join(s["summary_text"] for s in summaries)
    except Exception as e:
        logger.warning("context_enrichment_failed", error=str(e))
    return None


async def _log_memory_stats():
    """Log conversation history memory usage statistics."""
    total_messages = sum(len(h) for h in conversation_history.values())
    thread_count = len(conversation_history)
    logger.info(
        "memory_stats",
        thread_count=thread_count,
        total_messages=total_messages,
    )


async def _periodic_memory_stats():
    """Periodically log memory statistics."""
    while True:
        await asyncio.sleep(300)  # Log every 5 minutes
        await _log_memory_stats()


async def process_message(redis_client: RedisStreamClient, data: dict) -> None:
    """Process a single message through the LangGraph.

    Args:
        redis_client: Redis client for sending responses.
        data: Message data from Telegram.
    """
    telegram_user_id = data.get("user_id")  # This is telegram_id from Telegram API
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    correlation_id = data.get("correlation_id")

    # Get or create thread_id using Redis sequence
    # This replaces the simple f"user_{telegram_user_id}" format
    thread_id = await get_or_create_thread_id(telegram_user_id) if telegram_user_id else "unknown"

    # Resolve internal user_id from telegram_id
    internal_user_id = await _resolve_user_id(telegram_user_id) if telegram_user_id else None

    # Bind request context
    structlog.contextvars.bind_contextvars(
        thread_id=thread_id,
        correlation_id=correlation_id,
        telegram_user_id=telegram_user_id,
        user_id=internal_user_id,
    )

    logger.info("message_received", chat_id=chat_id, message_length=len(text))

    try:
        # Get existing conversation history
        history = conversation_history[thread_id]

        # Enrich context if history is empty
        if not history and internal_user_id:
            context = await _get_conversation_context(internal_user_id)
            if context:
                history.insert(
                    0,
                    SystemMessage(content=f"[Предыдущий контекст диалога]\n{context}"),
                )

        # Add new user message to history
        new_message = HumanMessage(content=text)
        history.append(new_message)

        # Prepare initial state with full history
        state: OrchestratorState = {
            "messages": list(history),  # Pass all history
            "current_project": None,
            "project_spec": None,
            "project_intent": None,
            "po_intent": None,
            "allocated_resources": {},
            "current_agent": "",
            "errors": [],
            "deployed_url": None,
            # User context for multi-tenancy
            "telegram_user_id": telegram_user_id,
            "user_id": internal_user_id,
        }

        # LangGraph config with thread_id for checkpointing
        config = {"configurable": {"thread_id": thread_id}}

        # Run the graph
        start_time = time.time()
        result = await graph.ainvoke(state, config)
        duration = (time.time() - start_time) * 1000

        # Get the last AI message
        messages = result.get("messages", [])
        if messages:
            last_message = messages[-1]
            if isinstance(last_message.content, list):
                # Handle list of blocks (text + image, etc.) - extract text
                response_text = "".join(
                    block["text"] for block in last_message.content if block.get("type") == "text"
                )
            else:
                response_text = str(last_message.content)

            # Save AI response to history
            history.append(AIMessage(content=response_text))

            # Trim history to keep only last N messages
            if len(history) > MAX_HISTORY_SIZE:
                conversation_history[thread_id] = history[-MAX_HISTORY_SIZE:]
        else:
            response_text = "Обработка завершена, но нет ответа."

        # Publish response to outgoing stream
        await redis_client.publish(
            RedisStreamClient.OUTGOING_STREAM,
            {
                "user_id": telegram_user_id,  # Telegram bot expects telegram_id
                "chat_id": chat_id,
                "reply_to_message_id": data.get("message_id"),
                "text": response_text,
                "correlation_id": correlation_id,
            },
        )

        logger.info(
            "response_sent", duration_ms=round(duration, 2), response_length=len(response_text)
        )

    except Exception as e:
        duration = (time.time() - start_time) * 1000 if "start_time" in locals() else 0
        logger.error(
            "message_processing_failed",
            error=str(e),
            error_type=type(e).__name__,
            duration_ms=round(duration, 2),
            exc_info=True,
        )

        # Clear conversation history to prevent corrupted state from persisting
        if thread_id in conversation_history:
            del conversation_history[thread_id]
            logger.info("conversation_history_cleared")

        # Send error message back
        await redis_client.publish(
            RedisStreamClient.OUTGOING_STREAM,
            {
                "chat_id": chat_id,
                "reply_to_message_id": data.get("message_id"),
                "text": f"⚠️ Произошла ошибка при обработке: {e!s}\n\n_История диалога очищена._",
            },
        )


PROVISIONER_TRIGGER_CHANNEL = "provisioner:trigger"
WORKER_EVENTS_ALL_CHANNEL = "worker:events:all"


async def listen_provisioner_triggers():
    """Listen for provisioning triggers from Redis pub/sub."""
    settings = get_settings()
    try:
        client = redis.from_url(settings.redis_url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(PROVISIONER_TRIGGER_CHANNEL)

        logger.info("provisioner_subscribed", channel=PROVISIONER_TRIGGER_CHANNEL)

        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    await process_provisioning_trigger(data)
                except Exception as e:
                    logger.error(
                        "provisioner_trigger_processing_failed",
                        error=str(e),
                        error_type=type(e).__name__,
                        exc_info=True,
                    )
    except asyncio.CancelledError:
        logger.info("provisioner_listener_cancelled")
    except Exception as e:
        logger.error(
            "provisioner_listener_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
    finally:
        await client.close()


async def listen_worker_events():
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


async def process_provisioning_trigger(data: dict) -> None:
    """Run the graph for provisioning."""
    server_handle = data.get("server_handle")
    is_incident_recovery = data.get("is_incident_recovery", False)

    if not server_handle:
        logger.warning("provisioner_trigger_missing_handle", payload=data)
        return

    structlog.contextvars.bind_contextvars(server_handle=server_handle, trigger="provisioner")

    logger.info("provisioner_trigger_received", is_incident_recovery=is_incident_recovery)

    now = datetime.now(UTC)
    if server_handle in active_provisioning:
        logger.info("provisioner_trigger_deduped", reason="active")
        structlog.contextvars.clear_contextvars()
        return

    last_complete = provisioning_cooldowns.get(server_handle)
    if last_complete and (now - last_complete) < timedelta(
        seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS
    ):
        logger.info(
            "provisioner_trigger_deduped",
            reason="cooldown",
            last_complete_at=last_complete.isoformat(),
            cooldown_seconds=PROVISIONING_TRIGGER_COOLDOWN_SECONDS,
        )
        structlog.contextvars.clear_contextvars()
        return

    active_provisioning.add(server_handle)

    state = {
        "messages": [HumanMessage(content=f"Provision server {server_handle}")],
        "server_to_provision": server_handle,
        "is_incident_recovery": is_incident_recovery,
        "current_agent": "provisioner",
        "errors": [],
        # Initialize required fields
        "current_project": None,
        "project_spec": None,
        "project_intent": None,
        "po_intent": None,
        "allocated_resources": {},
        "deployed_url": None,
        "repo_info": None,
        "architect_complete": False,
        "project_complexity": None,
        "provisioning_result": None,
    }

    config = {"configurable": {"thread_id": f"provisioner-{server_handle}"}}

    try:
        await graph.ainvoke(state, config)
        logger.info("provisioning_graph_complete")
    except Exception as e:
        logger.error("provisioning_graph_failed", error=str(e), exc_info=True)
    finally:
        active_provisioning.discard(server_handle)
        provisioning_cooldowns[server_handle] = datetime.now(UTC)
        structlog.contextvars.clear_contextvars()


async def consume_chat_stream():
    """Consume chat messages from Redis stream."""
    redis_client = RedisStreamClient()
    await redis_client.connect()

    logger.info("LangGraph chat consumer started...")

    try:
        async for message in redis_client.consume(
            stream=RedisStreamClient.INCOMING_STREAM,
            group="langgraph_workers",
            consumer="worker_1",
        ):
            # Process each message
            await process_message(redis_client, message.data)

    except asyncio.CancelledError:
        logger.info("Chat consumer shutdown requested")
    finally:
        await redis_client.close()


async def run_worker() -> None:
    """Run the LangGraph worker loop."""
    logger.info("Starting LangGraph worker services...")
    await asyncio.gather(
        consume_chat_stream(),
        listen_provisioner_triggers(),
        listen_worker_events(),
        _periodic_memory_stats(),
    )


def main() -> None:
    """Entry point for the worker."""
    setup_logging(service_name="langgraph")

    logger.info("Starting LangGraph worker...")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
