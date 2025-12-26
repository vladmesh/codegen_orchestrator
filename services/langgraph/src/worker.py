"""LangGraph worker - consumes messages from Redis and processes through graph."""

import asyncio
from collections import defaultdict
import json
import os
import sys
import time

from langchain_core.messages import AIMessage, HumanMessage
import redis.asyncio as redis
import structlog

from shared.logging_config import setup_logging

# Add shared to path
sys.path.insert(0, "/app")
from shared.redis_client import RedisStreamClient

from .graph import OrchestratorState, create_graph

logger = structlog.get_logger()

# In-memory conversation history cache
# Key: thread_id, Value: list of messages (last N messages)
MAX_HISTORY_SIZE = 6
conversation_history: dict[str, list] = defaultdict(list)

# Create graph once at startup (with MemorySaver)
graph = create_graph()


async def process_message(redis_client: RedisStreamClient, data: dict) -> None:
    """Process a single message through the LangGraph.

    Args:
        redis_client: Redis client for sending responses.
        data: Message data from Telegram.
    """
    user_id = data.get("user_id")
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    text = data.get("text", "")
    thread_id = data.get("thread_id", f"user_{user_id}")
    correlation_id = data.get("correlation_id")

    # Bind request context
    structlog.contextvars.bind_contextvars(
        thread_id=thread_id, correlation_id=correlation_id, user_id=user_id
    )

    logger.info("message_received", chat_id=chat_id, message_length=len(text))

    try:
        # Get existing conversation history
        history = conversation_history[thread_id]

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
                "chat_id": chat_id,
                "reply_to_message_id": data.get("message_id"),
                "text": response_text,
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


async def listen_provisioner_triggers():
    """Listen for provisioning triggers from Redis pub/sub."""
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    try:
        client = redis.from_url(redis_url, decode_responses=True)
        pubsub = client.pubsub()
        await pubsub.subscribe(PROVISIONER_TRIGGER_CHANNEL)

        logger.info(f"Subscribed to provisioning triggers on {PROVISIONER_TRIGGER_CHANNEL}")

        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    await process_provisioning_trigger(data)
                except Exception as e:
                    logger.error(f"Error processing trigger: {e}")
    except asyncio.CancelledError:
        logger.info("Provisioner listener cancelled")
    except Exception as e:
        logger.error(f"Provisioner listener failed: {e}")
    finally:
        await client.close()


async def process_provisioning_trigger(data: dict) -> None:
    """Run the graph for provisioning."""
    server_handle = data.get("server_handle")
    is_incident_recovery = data.get("is_incident_recovery", False)

    structlog.contextvars.bind_contextvars(server_handle=server_handle, trigger="provisioner")

    logger.info("provisioner_trigger_received", is_incident_recovery=is_incident_recovery)

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
    )


def main() -> None:
    """Entry point for the worker."""
    setup_logging(service_name="langgraph")

    logger.info("Starting LangGraph worker...")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
