"""PO ReactAgent Redis stream consumer.

Reads messages from po:input, invokes the PO ReactAgent graph,
writes responses to po:response:{request_id}.

NOTE: This consumer keeps its own while-loop (instead of using
RedisStreamClient.consume()) because it dispatches messages concurrently
via asyncio.create_task() with a semaphore and per-user locks.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import TypeAdapter, ValidationError
import structlog

from shared.contracts.queues.po import (
    POInputMessage,
    POProactiveMessage,
    POResponse,
    to_flat_fields,
)
from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE
from shared.redis_client import RedisStreamClient

from ..agents.po.graph import create_po_graph
from ..agents.po.tools import init_po_clients
from ..config.settings import get_settings

logger = structlog.get_logger(__name__)

MAX_CONCURRENT = 10
CONSUMER_NAME = f"po-worker-{os.getpid()}"
PEL_TIMEOUT_MS = 60_000

_po_input_adapter = TypeAdapter(POInputMessage)


async def _recover_pending(client: RedisStreamClient, sem, user_locks, graph) -> int:
    """Recover pending messages from PEL via XAUTOCLAIM before reading new ones."""
    recovered = 0
    cursor = "0-0"
    while True:
        result = await client.redis.xautoclaim(
            PO_INPUT_QUEUE,
            PO_CONSUMER_GROUP,
            CONSUMER_NAME,
            min_idle_time=PEL_TIMEOUT_MS,
            start_id=cursor,
            count=10,
        )
        new_cursor = result[0]
        claimed = result[1]
        for msg_id, fields in claimed:
            if fields is None:
                continue
            recovered += 1
            asyncio.create_task(_process_message(graph, client, sem, user_locks, msg_id, fields))
        if new_cursor == "0-0" or not claimed:
            break
        cursor = new_cursor
    if recovered:
        logger.info("po_pel_recovery_complete", recovered=recovered)
    return recovered


async def run_po_consumer() -> None:
    """Main loop: read po:input, invoke PO graph, write po:response:*."""
    settings = get_settings()
    client = RedisStreamClient(redis_url=settings.redis_url)
    await client.connect()
    redis = client.redis

    api_client = httpx.AsyncClient(
        base_url=settings.api_base_url.rstrip("/"),
        follow_redirects=True,
        timeout=30.0,
    )

    init_po_clients(api_client, client)

    graph = await create_po_graph(
        model=settings.po_llm_model,
        base_url=settings.po_llm_base_url,
        api_key=settings.po_llm_api_key,
        checkpoint_database_url=settings.checkpoint_database_url,
        summarization_model=settings.summarization_model,
        summarization_max_tokens=settings.summarization_max_tokens,
        summarization_trigger_tokens=settings.summarization_trigger_tokens,
        summarization_max_summary_tokens=settings.summarization_max_summary_tokens,
    )
    logger.info(
        "po_summarization_configured",
        model=settings.summarization_model or settings.po_llm_model,
        max_tokens=settings.summarization_max_tokens,
        trigger_tokens=settings.summarization_trigger_tokens,
    )

    # Ensure consumer group exists
    try:
        await redis.xgroup_create(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("po_consumer_group_created")
    except Exception as e:
        if "BUSYGROUP" in str(e):
            logger.debug("po_consumer_group_exists")
        else:
            raise

    logger.info("po_consumer_started", consumer=CONSUMER_NAME)

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    user_locks: dict[str, asyncio.Lock] = {}

    # PEL recovery on startup
    await _recover_pending(client, sem, user_locks, graph)

    try:
        while True:
            try:
                entries = await redis.xreadgroup(
                    PO_CONSUMER_GROUP,
                    CONSUMER_NAME,
                    {PO_INPUT_QUEUE: ">"},
                    count=10,
                    block=5000,
                )
            except asyncio.CancelledError:
                logger.info("po_consumer_cancelled")
                break
            except Exception as e:
                if "NOGROUP" in str(e):
                    logger.warning("po_consumer_nogroup_recovering")
                    try:
                        await redis.xgroup_create(
                            PO_INPUT_QUEUE, PO_CONSUMER_GROUP, id="0", mkstream=True
                        )
                    except Exception as create_err:
                        if "BUSYGROUP" not in str(create_err):
                            raise
                    await asyncio.sleep(1)
                    continue
                raise

            if not entries:
                continue

            for _stream_name, messages in entries:
                for msg_id, data in messages:
                    asyncio.create_task(
                        _process_message(graph, client, sem, user_locks, msg_id, data)
                    )
    finally:
        await api_client.aclose()
        await client.close()
        logger.info("po_consumer_shutdown")


async def _process_message(
    graph,
    client: RedisStreamClient,
    sem: asyncio.Semaphore,
    user_locks: dict[str, asyncio.Lock],
    msg_id: str,
    data: dict,
) -> None:
    """Process a single message with concurrency control."""
    # Validate incoming message
    try:
        _po_input_adapter.validate_python(data)
    except ValidationError:
        logger.warning("po_input_validation_failed", msg_id=msg_id, data=data)
        await client.redis.xack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, msg_id)
        return

    user_id = data.get("user_id", "unknown")
    lock = user_locks.setdefault(user_id, asyncio.Lock())

    async with sem:
        async with lock:
            try:
                await _handle_message(graph, client, user_id, data)
            except Exception:
                logger.exception("po_invoke_failed", user_id=user_id, msg_id=msg_id)
                request_id = data.get("request_id")
                if request_id:
                    error_resp = POResponse(
                        text="An error occurred, please try again.",
                        user_id=user_id,
                        error="true",
                    )
                    await client.publish_flat(
                        f"po:response:{request_id}",
                        to_flat_fields(error_resp),
                    )
            finally:
                await client.redis.xack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, msg_id)


async def _repair_orphan_tool_calls(graph, thread_id: str) -> int:
    """Detect and repair orphan tool_calls in checkpoint history.

    If an AIMessage has tool_calls without corresponding ToolMessages,
    inject recovery ToolMessages so the thread is no longer corrupted.
    Returns the number of repaired tool_calls.
    """
    config = {"configurable": {"thread_id": thread_id}}
    state = await graph.aget_state(config)
    messages = state.values.get("messages", [])
    if not messages:
        return 0

    tool_call_ids_with_results = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    orphan_calls = [
        tc
        for m in messages
        if isinstance(m, AIMessage)
        for tc in m.tool_calls
        if tc["id"] not in tool_call_ids_with_results
    ]
    if not orphan_calls:
        return 0

    recovery_messages = [
        ToolMessage(
            content="[recovery] Tool call interrupted — result unavailable.",
            tool_call_id=tc["id"],
        )
        for tc in orphan_calls
    ]
    await graph.aupdate_state(config, {"messages": recovery_messages})

    logger.warning(
        "po_checkpoint_repaired",
        thread_id=thread_id,
        repaired_count=len(orphan_calls),
        tool_names=[tc["name"] for tc in orphan_calls],
    )
    return len(orphan_calls)


async def _handle_message(graph, client: RedisStreamClient, user_id: str, data: dict) -> None:
    """Format message, invoke PO graph, write response."""
    timestamp = data.get("timestamp", "")
    text = data.get("text", "")
    msg_type = data.get("type", "user_message")
    event = data.get("event", "")

    # Let story-level events through to PO so it can craft user-friendly messages.
    # Drop all other system events — PO checks task-level status via reminders.
    _STORY_EVENTS = {"story_completed", "story_failed", "story_blocked"}
    if msg_type == "system_event" and event not in _STORY_EVENTS:
        logger.info("po_system_event_dropped", user_id=user_id, event_type=event, text=text)
        return

    user_name = data.get("user_name", "")

    formatted = f"[{timestamp} UTC] {text}" if timestamp else text

    if msg_type != "user_message":
        tag = f"{msg_type}:{event}" if event else msg_type
        formatted = f"[system: {tag}] {formatted}"
    else:
        # Inject user context so PO knows who it's talking to
        context_line = f"[context: user_id={user_id}, user_name={user_name}]"
        formatted = f"{context_line} {formatted}"
    msg = HumanMessage(content=formatted)
    thread_id = f"po-user-{user_id}"
    invoke_input = {"messages": [msg]}
    invoke_config = {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
            "user_name": user_name,
        },
        "recursion_limit": 50,
    }

    # Pre-invoke: repair any orphan tool_calls from previous crashed invocations
    await _repair_orphan_tool_calls(graph, thread_id)

    try:
        result = await graph.ainvoke(invoke_input, config=invoke_config)
    except ValueError as exc:
        if "tool_calls that do not have a corresponding ToolMessage" not in str(exc):
            raise
        # Race condition: corruption appeared between pre-check and invoke — repair and retry once
        logger.warning("po_checkpoint_corrupt_on_invoke", thread_id=thread_id, error=str(exc))
        await _repair_orphan_tool_calls(graph, thread_id)
        result = await graph.ainvoke(invoke_input, config=invoke_config)

    last_msg = result["messages"][-1]
    response_text = last_msg.content
    logger.debug(
        "po_graph_result",
        last_msg_type=type(last_msg).__name__,
        content_length=len(response_text) if response_text else 0,
        total_messages=len(result["messages"]),
    )

    request_id = data.get("request_id")
    if request_id:
        # Synchronous response — telegram bot is waiting
        if not response_text:
            response_text = "Бот вернул пустой ответ"
            logger.warning("po_empty_response_fallback", user_id=user_id, request_id=request_id)
        resp = POResponse(text=response_text, user_id=user_id)
        await client.publish_flat(f"po:response:{request_id}", to_flat_fields(resp))
    elif response_text:
        # No request_id (reminder, system event) — forward to user via proactive stream
        proactive = POProactiveMessage(text=response_text, user_id=user_id)
        await client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(proactive))

    logger.info(
        "po_message_handled",
        user_id=user_id,
        msg_type=msg_type,
        response_empty=not bool(response_text),
        has_request_id=bool(request_id),
    )
