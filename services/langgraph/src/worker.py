"""LangGraph worker - consumes messages from Redis and processes through graph."""

import asyncio
import logging
import sys
from collections import defaultdict

from langchain_core.messages import AIMessage, HumanMessage

# Add shared to path
sys.path.insert(0, "/app")
from shared.redis_client import RedisStreamClient

from .graph import OrchestratorState, create_graph

logger = logging.getLogger(__name__)

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
    thread_id = data.get("thread_id", f"user_{user_id}")

    logger.info(f"Processing message from user {user_id}: {text[:50]}...")

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
            "allocated_resources": {},
            "current_agent": "",
            "errors": [],
            "deployed_url": None,
        }

        # LangGraph config with thread_id for checkpointing
        config = {"configurable": {"thread_id": thread_id}}

        # Run the graph
        result = await graph.ainvoke(state, config)

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

        logger.info(f"Sent response to user {user_id}")

    except Exception as e:
        logger.exception(f"Error processing message from user {user_id}: {e}")

        # Send error message back
        await redis_client.publish(
            RedisStreamClient.OUTGOING_STREAM,
            {
                "chat_id": chat_id,
                "reply_to_message_id": data.get("message_id"),
                "text": f"⚠️ Произошла ошибка при обработке: {e!s}",
            },
        )


async def run_worker() -> None:
    """Run the LangGraph worker loop."""
    redis_client = RedisStreamClient()
    await redis_client.connect()

    logger.info("LangGraph worker started, consuming from Redis Stream...")

    try:
        async for message in redis_client.consume(
            stream=RedisStreamClient.INCOMING_STREAM,
            group="langgraph_workers",
            consumer="worker_1",
        ):
            # Process each message
            await process_message(redis_client, message.data)

    except asyncio.CancelledError:
        logger.info("Worker shutdown requested")
    finally:
        await redis_client.close()
        logger.info("Worker stopped")


def main() -> None:
    """Entry point for the worker."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Starting LangGraph worker...")
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
