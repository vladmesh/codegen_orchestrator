"""The owner of a project is reached in their real Telegram chat.

``Project.owner_id`` is an internal ``User.id``; Telegram addresses
``User.telegram_id``. The two used to travel in one field, so a pipeline-born
notification was sent to a chat id that did not exist. This test walks the whole
path with the two numbers deliberately far apart — ``User.id`` is whatever the
database assigns, ``telegram_id`` is 987654321 — and fails if the internal id
ever reaches the transport:

    scheduler producer (real API, real DB)
        → po:input (real Redis)
        → the PO mapping that turns an input event into a notification
        → po:proactive (real Redis)
        → the bot's delivery, whose Telegram call is captured
"""

from __future__ import annotations

import importlib.util
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
import structlog

# The integration runner has PYTHONPATH=/app with services/ mounted; the
# scheduler's modules import each other as `src.*`, so its directory goes on the
# path the same way the PO tools suite does it.
_SCHEDULER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "services", "scheduler")
)
if _SCHEDULER_DIR not in sys.path:
    sys.path.insert(0, _SCHEDULER_DIR)

from shared.contracts.queues.po import (  # noqa: E402
    POProactiveMessage,
    from_flat_fields,
    po_thread_id,
    proactive_from_input,
    to_flat_fields,
)
from shared.queues import (  # noqa: E402
    PO_INPUT_QUEUE,
    PO_PROACTIVE_GROUP,
    PO_PROACTIVE_QUEUE,
)
from shared.redis.client import RedisStreamClient, decode_redis_fields  # noqa: E402

OWNER_TELEGRAM_ID = 987654321
API_BASE_URL = os.getenv("API_BASE_URL", "http://172.31.0.20:8000")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


def _load_bot_delivery():
    """Load the bot's delivery module by path.

    It is a telegram_bot module and imports nothing from ``telegram``; loading it
    by path keeps this process free of a second package also called ``src``.
    """
    path = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "services",
            "telegram_bot",
            "src",
            "proactive.py",
        )
    )
    spec = importlib.util.spec_from_file_location("telegram_bot_proactive", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def scheduler_api():
    """The real scheduler API client, pointed at the real API service."""
    from src.clients.api import SchedulerAPIClient

    client = SchedulerAPIClient()
    yield client
    await client.close()


@pytest.fixture
async def stream_client():
    client = RedisStreamClient(REDIS_URL)
    await client.connect()
    # Each test reads what it published; the streams are per-run scratch.
    await client.redis.delete(PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE)
    yield client
    await client.close()


async def _owner_and_project(api_client) -> tuple[int, str]:
    """Register the owner and a project, returning (internal user id, project id)."""
    resp = await api_client.post(
        "/api/users/upsert",
        json={
            "telegram_id": OWNER_TELEGRAM_ID,
            "username": "recipient-owner",
            "first_name": "Recipient",
            "is_admin": False,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    user_id = resp.json()["id"]

    resp = await api_client.post(
        "/api/projects/",
        headers={"X-Telegram-ID": str(OWNER_TELEGRAM_ID)},
        json={"initiating_run_id": "test-run-1", "title": f"recipient-{uuid.uuid4().hex[:6]}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return user_id, resp.json()["id"]


async def _read_one_proactive_entry(client: RedisStreamClient, *, claim_pending: bool = False):
    """Read one po:proactive entry through the group, without acking it."""
    async for msg in client.consume(
        PO_PROACTIVE_QUEUE,
        PO_PROACTIVE_GROUP,
        "bot-0",
        count=1,
        block_ms=1000,
        auto_ack=False,
        claim_pending=claim_pending,
        pending_timeout_ms=0,
    ):
        if msg is not None:
            return msg
    raise AssertionError("po:proactive had nothing to read")


async def _pending_count(client: RedisStreamClient) -> int:
    entries = await client.redis.xpending_range(
        PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP, min="-", max="+", count=10
    )
    return len(entries)


@pytest.mark.asyncio
async def test_pipeline_event_reaches_the_owner_telegram_chat(
    api_client, scheduler_api, stream_client
):
    from src.tasks.supervisor.liveness import _request_resources_via_po

    user_id, project_id = await _owner_and_project(api_client)
    # The whole point of the card: these are different numbers, and only one of
    # them addresses a chat.
    assert user_id != OWNER_TELEGRAM_ID

    task = SimpleNamespace(id="task-recipient-1", project_id=project_id, story_id="story-recip-1")
    await _request_resources_via_po(
        scheduler_api, stream_client, task, structlog.get_logger(__name__)
    )

    entries = await stream_client.redis.xrange(PO_INPUT_QUEUE)
    assert len(entries) == 1, "the scheduler published exactly one PO event"
    event = decode_redis_fields(entries[0][1])
    assert event["event"] == "task_waiting_resources"
    assert event["telegram_chat_id"] == str(OWNER_TELEGRAM_ID)
    assert event["owner_user_id"] == str(user_id)

    # PO answers into the same thread the user's own messages use, and the
    # notification it emits keeps the resolved chat.
    assert po_thread_id(event["telegram_chat_id"]) == po_thread_id(str(OWNER_TELEGRAM_ID))
    proactive = proactive_from_input(
        event, "Engineering is waiting for server capacity.", event["telegram_chat_id"]
    )
    await stream_client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(proactive))

    published = await stream_client.redis.xrange(PO_PROACTIVE_QUEUE)
    assert len(published) == 1
    message = from_flat_fields(decode_redis_fields(published[0][1]), POProactiveMessage)
    assert message.telegram_chat_id == str(OWNER_TELEGRAM_ID)
    assert message.owner_user_id == str(user_id)

    delivery = _load_bot_delivery()
    bot = AsyncMock()
    entry = await _read_one_proactive_entry(stream_client)
    outcome = await delivery.process_proactive_entry(bot, stream_client, entry)

    assert outcome is delivery.ProactiveOutcome.DELIVERED
    chat_id = bot.send_message.await_args.kwargs["chat_id"]
    assert chat_id == OWNER_TELEGRAM_ID
    # The regression this test exists for: the internal id must never be the
    # destination.
    assert chat_id != user_id
    assert await _pending_count(stream_client) == 0, "a delivered entry is acked"


@pytest.mark.asyncio
async def test_a_delivery_abandoned_by_a_bot_restart_still_reaches_the_owner(
    api_client, stream_client
):
    """The bot dying mid-send must not swallow the notification.

    The entry stays in the consumer group's pending list; the next bot claims it
    and delivers it to the same chat. Real Redis, real consumer group.
    """
    user_id, project_id = await _owner_and_project(api_client)
    proactive = POProactiveMessage(
        text="Your project is deployed",
        telegram_chat_id=str(OWNER_TELEGRAM_ID),
        owner_user_id=str(user_id),
        event="story_completed",
        story_id="story-recip-2",
        project_id=project_id,
    )
    await stream_client.ensure_consumer_group(PO_PROACTIVE_QUEUE, PO_PROACTIVE_GROUP)
    await stream_client.publish_flat(PO_PROACTIVE_QUEUE, to_flat_fields(proactive))

    # First delivery: handed out, never settled — the bot was killed.
    await stream_client.redis.xreadgroup(
        groupname=PO_PROACTIVE_GROUP,
        consumername="bot-0",
        streams={PO_PROACTIVE_QUEUE: ">"},
        count=10,
        block=0,
    )
    assert await _pending_count(stream_client) == 1

    delivery = _load_bot_delivery()
    bot = AsyncMock()
    entry = await _read_one_proactive_entry(stream_client, claim_pending=True)
    outcome = await delivery.process_proactive_entry(bot, stream_client, entry)

    assert outcome is delivery.ProactiveOutcome.DELIVERED
    assert bot.send_message.await_args.kwargs["chat_id"] == OWNER_TELEGRAM_ID
    assert await _pending_count(stream_client) == 0
