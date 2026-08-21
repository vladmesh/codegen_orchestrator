"""PO ReactAgent Redis stream consumer.

Reads messages from po:input, invokes the PO ReactAgent graph,
writes responses to po:response:{request_id}.

PO reads through ``RedisStreamClient.consume_typed`` like every other consumer:
the recurring PEL sweep, the full PEL walk, the trim diagnostics, the DLQ route
for a poison entry and the tolerance for a field a newer publisher added all
live there and are not reimplemented here.

What is PO's own is dispatch. Every entry goes to an ``asyncio.Task`` under a
semaphore and a per-user lock, and is ACKed in that task's ``finally``, so an
entry stays pending for as long as the graph runs — minutes, legitimately. A
recurring sweep hands such an entry straight back to the process that is still
working on it, which is why this module keeps two things beside the shared loop:

- ``RECLAIM_INTERVAL_MS`` is strictly shorter than ``PEL_TIMEOUT_MS``, so the
  sweep is also the lease renewal for this process's in-flight entries: while
  PO is running they do not go idle for a whole ``PEL_TIMEOUT_MS``, which is
  the bar any consumer's sweep measures them against. When PO dies the renewal
  stops with it and the entry becomes claimable after one lease.
- ``_consume_po_input`` remembers which ids are in flight *here* and does not
  dispatch a second ``_process_message`` for one the sweep just renewed.
"""

from __future__ import annotations

import asyncio
import os

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import TypeAdapter
import structlog

from shared.contracts.queues.po import (
    POInputMessage,
    POResponse,
    po_thread_id,
    proactive_from_input,
    to_flat_fields,
)
from shared.log_config.correlation import bind_message_context, unbind_message_context
from shared.notifications import notify_admins_best_effort
from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE
from shared.redis_client import RedisStreamClient

from ..agents.po.graph import create_po_graph
from ..agents.po.tools import init_po_clients
from ..clients.api import api_client
from ..config.settings import get_settings

logger = structlog.get_logger(__name__)

MAX_CONCURRENT = 10
CONSUMER_NAME = f"po-worker-{os.getpid()}"

# How long an entry must go undelivered before a sweep may take it. It is also
# the lease a live PO holds on the work it is running.
PEL_TIMEOUT_MS = 60_000

# How often the sweep comes round. Half the lease rather than the shared
# client's default of one full lease, because here the sweep renews this
# process's own in-flight entries: renewing at exactly the moment they become
# claimable would be a race with any other consumer's sweep, and renewing more
# slowly than the lease would lose them.
RECLAIM_INTERVAL_MS = PEL_TIMEOUT_MS // 2

READ_BLOCK_MS = 5_000
READ_COUNT = 10

_po_input_adapter = TypeAdapter(POInputMessage)


async def _consume_po_input(
    graph,
    client: RedisStreamClient,
    sem: asyncio.Semaphore,
    user_locks: dict[str, asyncio.Lock],
) -> None:
    """Read po:input through the shared client and dispatch each entry.

    ``in_flight`` maps an entry id to the task processing it. It holds the only
    strong reference to that task, and it is what tells a reclaimed entry that
    belongs to work already running here apart from one that needs dispatching.
    An id leaves it when the task finishes, which is after the ACK: between the
    two the entry is gone from the PEL anyway, so the sweep cannot offer it.
    """
    in_flight: dict[str, asyncio.Task] = {}

    def _dispatched(task: asyncio.Task, msg_id: str) -> None:
        in_flight.pop(msg_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("po_dispatch_failed", msg_id=msg_id, error=str(task.exception()))

    async for message in client.consume_typed(
        PO_INPUT_QUEUE,
        PO_CONSUMER_GROUP,
        CONSUMER_NAME,
        _po_input_adapter,
        block_ms=READ_BLOCK_MS,
        count=READ_COUNT,
        claim_pending=True,
        pending_timeout_ms=PEL_TIMEOUT_MS,
        reclaim_interval_ms=RECLAIM_INTERVAL_MS,
    ):
        if message is None:
            continue
        if message.message_id in in_flight:
            # The sweep just renewed our lease on work this process is still
            # running. Renewal is the point of it reaching us; a second
            # _process_message for the same entry would double that work.
            logger.debug("po_in_flight_entry_renewed", msg_id=message.message_id)
            continue
        task = asyncio.create_task(
            _process_message(graph, client, sem, user_locks, message.message_id, message.value)
        )
        in_flight[message.message_id] = task
        task.add_done_callback(lambda done, msg_id=message.message_id: _dispatched(done, msg_id))


async def run_po_consumer() -> None:
    """Main loop: read po:input, invoke PO graph, write po:response:*."""
    settings = get_settings()
    client = RedisStreamClient(redis_url=settings.redis_url)
    await client.connect()

    init_po_clients(api_client, client)

    # Read summarization config from DB (ConfigStore), fall back to settings
    from shared.config_store import ConfigStore

    try:
        _cfg = ConfigStore(settings.api_base_url)
        _sum_max = _cfg.get_int(
            "llm.summarization_max_tokens", default=settings.summarization_max_tokens
        )
        _sum_trigger = _cfg.get_int(
            "llm.summarization_trigger_tokens", default=settings.summarization_trigger_tokens
        )
        _sum_max_summary = _cfg.get_int(
            "llm.summarization_max_summary_tokens",
            default=settings.summarization_max_summary_tokens,
        )
    except Exception:
        _sum_max = settings.summarization_max_tokens
        _sum_trigger = settings.summarization_trigger_tokens
        _sum_max_summary = settings.summarization_max_summary_tokens

    graph = await create_po_graph(
        model=settings.po_llm_model,
        base_url=settings.po_llm_base_url,
        api_key=settings.po_llm_api_key,
        checkpoint_database_url=settings.checkpoint_database_url,
        summarization_model=settings.summarization_model,
        summarization_max_tokens=_sum_max,
        summarization_trigger_tokens=_sum_trigger,
        summarization_max_summary_tokens=_sum_max_summary,
    )
    logger.info(
        "po_summarization_configured",
        model=settings.summarization_model or settings.po_llm_model,
        max_tokens=settings.summarization_max_tokens,
        trigger_tokens=settings.summarization_trigger_tokens,
    )

    logger.info("po_consumer_started", consumer=CONSUMER_NAME)

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    user_locks: dict[str, asyncio.Lock] = {}

    try:
        # The consumer group, the recurring PEL sweep and the NOGROUP recovery
        # are the shared client's; PO only decides what to do with an entry.
        await _consume_po_input(graph, client, sem, user_locks)
    finally:
        await api_client.close()
        await client.close()
        logger.info("po_consumer_shutdown")


async def _process_message(
    graph,
    client: RedisStreamClient,
    sem: asyncio.Semaphore,
    user_locks: dict[str, asyncio.Lock],
    msg_id: str,
    message: POInputMessage,
) -> None:
    """Process one validated message with concurrency control.

    Everything that can go wrong before this point — a body that will not
    decode, one that fails ``POInputMessage``, one still addressed by the
    removed ``user_id`` field — is handled by ``consume_typed``: it logs with
    values elided, alerts, copies the entry to ``po:input:dlq`` and only then
    ACKs it. So what arrives here is a model, and the ACK below is the one for
    work that was actually attempted.
    """
    data = message.model_dump(mode="json")
    bind_message_context(data)
    # The addressing key is the Telegram chat, never the internal user id: it is
    # what the per-user lock and the PO thread are keyed by, so a pipeline event
    # about a project lands in the same conversation the user is typing in.
    telegram_chat_id = message.telegram_chat_id
    lock = user_locks.setdefault(telegram_chat_id, asyncio.Lock())

    async with sem:
        async with lock:
            try:
                await _handle_message(graph, client, telegram_chat_id, data)
            except Exception:
                logger.exception(
                    "po_invoke_failed", telegram_chat_id=telegram_chat_id, msg_id=msg_id
                )
                request_id = data.get("request_id")
                if request_id:
                    error_resp = POResponse(
                        text="An error occurred, please try again.",
                        telegram_chat_id=telegram_chat_id,
                        error="true",
                    )
                    await client.publish_flat(
                        f"po:response:{request_id}",
                        to_flat_fields(error_resp),
                    )
            finally:
                await client.redis.xack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, msg_id)
                unbind_message_context()


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


async def _handle_message(
    graph, client: RedisStreamClient, telegram_chat_id: str, data: dict
) -> None:
    """Format message, invoke PO graph, write response."""
    timestamp = data.get("timestamp", "")
    text = data.get("text", "")
    msg_type = data.get("type", "user_message")
    event = data.get("event", "")

    # Let user-facing lifecycle events through so PO can craft their wording.
    # Drop other system events — PO checks ordinary task status via reminders.
    _STORY_EVENTS = {
        "story_completed",
        "story_failed",
        "story_blocked",
        "story_quarantined",
        "story_waiting_user_secret",
        "task_waiting_resources",
        "task_waiting_infrastructure",
        "task_impossible_capacity",
        "story_impossible_capacity",
        "task_resources_resumed",
    }
    if msg_type == "system_event" and event not in _STORY_EVENTS:
        logger.info(
            "po_system_event_dropped",
            telegram_chat_id=telegram_chat_id,
            event_type=event,
            text=text,
        )
        return

    # A user-facing event whose recipient was never resolved cannot be delivered
    # and must not be answered into a thread keyed by nothing. Producers resolve
    # the chat before publishing, so reaching here is a defect worth an alert.
    if not telegram_chat_id:
        logger.error(
            "po_message_without_recipient",
            msg_type=msg_type,
            event_type=event,
            story_id=data.get("story_id", ""),
            project_id=data.get("project_id", ""),
        )
        await notify_admins_best_effort(
            f"PO event has no Telegram recipient: event={event or msg_type} "
            f"story={data.get('story_id') or '-'} project={data.get('project_id') or '-'} "
            f"owner_user_id={data.get('owner_user_id') or '-'}",
            level="error",
            po_event=event or msg_type,
            story_id=data.get("story_id", ""),
            project_id=data.get("project_id", ""),
        )
        return

    user_name = data.get("user_name", "")

    formatted = f"[{timestamp} UTC] {text}" if timestamp else text

    if msg_type != "user_message":
        tag = f"{msg_type}:{event}" if event else msg_type
        formatted = f"[system: {tag}] {formatted}"
    else:
        # Inject user context so PO knows who it's talking to
        context_line = f"[context: telegram_chat_id={telegram_chat_id}, user_name={user_name}]"
        formatted = f"{context_line} {formatted}"
    msg = HumanMessage(content=formatted)
    # One thread per Telegram chat: both the user's own messages and the events
    # the pipeline raises about their projects resolve to the same key.
    thread_id = po_thread_id(telegram_chat_id)
    invoke_input = {"messages": [msg]}
    invoke_config = {
        "configurable": {
            "thread_id": thread_id,
            "telegram_chat_id": telegram_chat_id,
            "user_name": user_name,
            "retry_story_id": data.get("story_id", ""),
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
            logger.warning(
                "po_empty_response_fallback",
                telegram_chat_id=telegram_chat_id,
                request_id=request_id,
            )
        resp = POResponse(text=response_text, telegram_chat_id=telegram_chat_id)
        await client.publish_flat(f"po:response:{request_id}", to_flat_fields(resp))
    elif response_text:
        # No request_id (reminder, system event) — forward to user via proactive
        # stream, carrying the identifiers the transport needs if delivery fails.
        proactive = proactive_from_input(data, response_text, telegram_chat_id)
        await client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(proactive))

    logger.info(
        "po_message_handled",
        telegram_chat_id=telegram_chat_id,
        msg_type=msg_type,
        response_empty=not bool(response_text),
        has_request_id=bool(request_id),
    )
