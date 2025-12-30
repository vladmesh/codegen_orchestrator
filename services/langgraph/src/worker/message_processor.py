"""Message processing through LangGraph."""

import asyncio
import sys
import time

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
import structlog

# Add shared to path
sys.path.insert(0, "/app")
from shared.redis_client import RedisStreamClient

from ..clients.api import api_client
from ..graph import OrchestratorState, create_graph
from ..session_manager import SessionState, session_manager
from ..thread_manager import get_current_thread_id
from .utils import MAX_HISTORY_SIZE, conversation_history

logger = structlog.get_logger()

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


async def _has_active_session(user_id: int) -> tuple[bool, str | None]:
    """Check if user has an active session (conversation history exists).

    For now, we just check if there's conversation history for the current thread.
    In the future, this could check LangGraph checkpointer directly.

    Returns:
        Tuple of (has_active_session, current_thread_id)
    """
    thread_id = await get_current_thread_id(user_id)
    if thread_id is None:
        return False, None

    # Check if there's any conversation history for this thread
    has_history = bool(conversation_history.get(thread_id))

    return has_history, thread_id


async def _handle_session_lock(
    telegram_user_id: int,
    chat_id: int,
    correlation_id: str | None,
    redis_client: RedisStreamClient,
) -> tuple[str | None, bool]:
    """Check session lock. Returns (thread_id, skip_intent_parser).

    Returns (None, False) if session is locked/busy (and handles rejection).
    """
    is_locked, lock_state = await session_manager.is_locked(telegram_user_id)

    if is_locked:
        if lock_state == SessionState.PROCESSING:
            # Reject - system is busy processing previous request
            await redis_client.publish(
                RedisStreamClient.OUTGOING_STREAM,
                {
                    "user_id": telegram_user_id,
                    "chat_id": chat_id,
                    "text": "⏳ Подожди, я ещё обрабатываю предыдущий запрос...",
                    "correlation_id": correlation_id,
                },
            )
            logger.info("message_rejected_busy", telegram_user_id=telegram_user_id)
            return None, False

        if lock_state == SessionState.AWAITING:
            # Continue existing session
            thread_id = await session_manager.continue_session(telegram_user_id)
            logger.info(
                "session_continued",
                telegram_user_id=telegram_user_id,
                thread_id=thread_id,
            )
            return thread_id, True

        # Unknown state, treat as new session
        thread_id = await session_manager.start_new_session(telegram_user_id)
        return thread_id, False

    # No active session - start new
    thread_id = await session_manager.start_new_session(telegram_user_id)
    return thread_id, False


async def _handle_execution_result(
    result: dict,
    telegram_user_id: int,
    thread_id: str,
    message_id: int | None,
    chat_id: int,
    correlation_id: str | None,
    history: list,
    duration: float,
    redis_client: RedisStreamClient,
) -> None:
    """Update session state and send response based on execution result."""
    # === Update Session State ===
    if result.get("user_confirmed_complete"):
        # Task complete - release lock, clear history
        await session_manager.release_lock(telegram_user_id)
        if thread_id in conversation_history:
            del conversation_history[thread_id]
        logger.info("session_completed", thread_id=thread_id)

    elif result.get("awaiting_user_response"):
        # Waiting for user - update state to AWAITING
        await session_manager.update_state(telegram_user_id, SessionState.AWAITING)
        logger.info("session_awaiting_user", thread_id=thread_id)

    else:
        # Graph ended but not complete and not awaiting (e.g. max iterations)
        await session_manager.release_lock(telegram_user_id)
        logger.info("session_ended_naturally", thread_id=thread_id)

    # === Send Response ===
    messages = result.get("messages", [])
    response_text = "Обработка завершена, но нет ответа."

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

    # Publish response
    await redis_client.publish(
        RedisStreamClient.OUTGOING_STREAM,
        {
            "user_id": telegram_user_id,
            "chat_id": chat_id,
            "reply_to_message_id": message_id,
            "text": response_text,
            "correlation_id": correlation_id,
        },
    )

    logger.info(
        "response_sent",
        duration_ms=round(duration, 2),
        response_length=len(response_text),
    )


async def process_message(redis_client: RedisStreamClient, data: dict) -> None:
    """Process a single message through the LangGraph.

    Flow (Phase 5 with SessionManager):
    1. Check session lock
    2. Run graph
    3. Update session state based on result
    """
    telegram_user_id = data.get("user_id")  # This is telegram_id from Telegram API
    chat_id = data.get("chat_id")
    text = data.get("text", "")
    correlation_id = data.get("correlation_id")

    # === Phase 5: Session Lock Check ===
    thread_id, skip_intent_parser = await _handle_session_lock(
        telegram_user_id, chat_id, correlation_id, redis_client
    )
    if thread_id is None:
        return

    # Resolve internal user_id from telegram_id
    internal_user_id = await _resolve_user_id(telegram_user_id) if telegram_user_id else None

    # Bind request context
    structlog.contextvars.bind_contextvars(
        thread_id=thread_id,
        correlation_id=correlation_id,
        telegram_user_id=telegram_user_id,
        user_id=internal_user_id,
    )

    logger.info(
        "message_received",
        chat_id=chat_id,
        message_length=len(text),
        skip_intent_parser=skip_intent_parser,
    )

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
            # Dynamic PO Phase 2: control whether to run intent parser
            "skip_intent_parser": skip_intent_parser,
            "thread_id": thread_id,
            "active_capabilities": [],
            "task_summary": None,
            # Dynamic PO Phase 3: agentic loop control
            "chat_id": chat_id,
            "correlation_id": correlation_id,
            "awaiting_user_response": False,  # Reset on each new message
            "user_confirmed_complete": False,
            "po_iterations": 0,
        }

        # LangGraph config with thread_id for checkpointing
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}

        # Run the graph
        start_time = time.time()
        result = await graph.ainvoke(state, config)
        duration = (time.time() - start_time) * 1000

        # === Phase 5: Update Session State & Send Response ===
        await _handle_execution_result(
            result,
            telegram_user_id,
            thread_id,
            data.get("message_id"),
            chat_id,
            correlation_id,
            history,
            duration,
            redis_client,
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

        # Phase 5: Release lock on error to prevent stuck sessions
        await session_manager.release_lock(telegram_user_id)

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


async def consume_chat_stream() -> None:
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
