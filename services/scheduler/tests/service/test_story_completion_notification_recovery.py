"""A direct API completion reaches the scheduler recovery seam.

This is the operator path: no QA run exists, so only the story-backed record
can carry the PO instruction across the API/scheduler process boundary.
"""

import os
import uuid

import httpx
import pytest

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.queues.po import POSystemEvent, from_flat_fields
from shared.queues import PO_INPUT_QUEUE
from shared.redis_client import RedisStreamClient


@pytest.mark.asyncio
async def test_direct_completion_without_qa_is_recovered_to_po_input(api_client):
    from src.tasks.owner_notifications import supervise_owed_owner_notifications

    project_id = str(uuid.uuid4())
    telegram_id = uuid.uuid4().int % 1_000_000_000
    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    async with httpx.AsyncClient(
        base_url=api_client.base_url, headers=headers, timeout=30.0
    ) as client:
        user = await client.post(
            "/api/users/",
            json={"telegram_id": telegram_id, "username": f"completion_{telegram_id}"},
        )
        assert user.status_code == httpx.codes.CREATED, user.text
        project = await client.post(
            "/api/projects/",
            json={
                "id": project_id,
                "initiating_run_id": "test-run-1",
                "title": "Direct completion recovery",
                "config": {},
            },
            headers={**headers, "X-Telegram-ID": str(telegram_id)},
        )
        assert project.status_code == httpx.codes.CREATED, project.text
        created = await client.post(
            "/api/stories/", json={"project_id": project_id, "title": "Ship direct completion"}
        )
        assert created.status_code == httpx.codes.CREATED, created.text
        story_id = created.json()["id"]
        started = await client.post(f"/api/stories/{story_id}/start")
        assert started.status_code == httpx.codes.OK, started.text

        # No QA run is created. This is the bare operator action the next card
        # depends on, and it must write durable work before it returns.
        completed = await client.post(f"/api/stories/{story_id}/complete")
        assert completed.status_code == httpx.codes.OK, completed.text
        notification = OwnerNotification.model_validate(
            (await client.get(f"/api/stories/{story_id}/owner-notification")).json()
        )
        assert notification.state is OwnerNotificationState.OWED

    redis_client = RedisStreamClient(os.environ["REDIS_URL"])
    await redis_client.connect()
    try:
        newest = await redis_client.redis.xrevrange(PO_INPUT_QUEUE, count=1)
        before = newest[0][0] if newest else "0-0"

        counts = await supervise_owed_owner_notifications(api_client, redis_client)
        assert counts["delivered"] >= 1

        unread = await redis_client.redis.xread({PO_INPUT_QUEUE: before})
        events = [
            from_flat_fields(fields, POSystemEvent)
            for _, entries in unread
            for _, fields in entries
            if fields.get("type") == "system_event"
        ]
        event = next(item for item in events if item.story_id == story_id)
        assert event.event == "story_completed"
        assert event.text == notification.text
        assert event.telegram_chat_id == str(telegram_id)
        assert event.task_id == story_id

        settled = OwnerNotification.model_validate(
            (await api_client.request("GET", f"stories/{story_id}/owner-notification")).json()
        )
        assert settled.state is OwnerNotificationState.DELIVERED
    finally:
        await redis_client.close()
