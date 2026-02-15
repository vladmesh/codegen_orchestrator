"""PO ReactAgent Redis stream consumer.

Reads messages from po:input, invokes the PO ReactAgent graph,
writes responses to po:response:{request_id}.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
import redis.asyncio as aioredis
import structlog

from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE

from ..config.settings import get_settings
from .graph import create_po_graph
from .tools import init_po_clients

logger = structlog.get_logger(__name__)

MAX_CONCURRENT = 10
CONSUMER_NAME = f"po-worker-{os.getpid()}"


async def run_po_consumer() -> None:
    """Main loop: read po:input, invoke PO graph, write po:response:*."""
    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    api_client = httpx.AsyncClient(
        base_url=settings.api_base_url.rstrip("/"),
        follow_redirects=True,
        timeout=30.0,
    )

    init_po_clients(api_client, redis)

    graph = await create_po_graph(
        model=settings.po_llm_model,
        base_url=settings.po_llm_base_url,
        api_key=settings.po_llm_api_key,
        checkpoint_database_url=settings.checkpoint_database_url,
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

            if not entries:
                continue

            for _stream_name, messages in entries:
                for msg_id, data in messages:
                    asyncio.create_task(
                        _process_message(graph, redis, sem, user_locks, msg_id, data)
                    )
    finally:
        await api_client.aclose()
        await redis.aclose()
        logger.info("po_consumer_shutdown")


async def _process_message(
    graph,
    redis: aioredis.Redis,
    sem: asyncio.Semaphore,
    user_locks: dict[str, asyncio.Lock],
    msg_id: str,
    data: dict,
) -> None:
    """Process a single message with concurrency control."""
    user_id = data.get("user_id", "unknown")
    lock = user_locks.setdefault(user_id, asyncio.Lock())

    async with sem:
        async with lock:
            try:
                await _handle_message(graph, redis, user_id, data)
            except Exception:
                logger.exception("po_invoke_failed", user_id=user_id, msg_id=msg_id)
                request_id = data.get("request_id")
                if request_id:
                    await redis.xadd(
                        f"po:response:{request_id}",
                        {
                            "text": "An error occurred, please try again.",
                            "user_id": user_id,
                            "error": "true",
                        },
                    )
            finally:
                await redis.xack(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, msg_id)


async def _handle_message(graph, redis: aioredis.Redis, user_id: str, data: dict) -> None:
    """Format message, invoke PO graph, write response."""
    timestamp = data.get("timestamp", "")
    text = data.get("text", "")
    msg_type = data.get("type", "user_message")

    formatted = f"[{timestamp} UTC] {text}" if timestamp else text

    if msg_type == "user_message":
        msg = HumanMessage(content=formatted)
    else:
        msg = SystemMessage(content=formatted)

    result = await graph.ainvoke(
        {"messages": [msg]},
        config={
            "configurable": {
                "thread_id": f"po-user-{user_id}",
                "user_id": user_id,
            }
        },
    )

    response_text = result["messages"][-1].content

    request_id = data.get("request_id")
    if request_id:
        await redis.xadd(
            f"po:response:{request_id}",
            {"text": response_text, "user_id": user_id},
        )

    logger.info(
        "po_message_handled",
        user_id=user_id,
        msg_type=msg_type,
        has_request_id=bool(request_id),
    )
