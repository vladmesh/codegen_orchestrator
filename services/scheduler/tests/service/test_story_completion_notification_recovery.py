"""A direct API completion reaches the scheduler recovery seam.

This is the operator path: no QA run exists, so only the story-backed record
can carry the PO instruction across the API/scheduler process boundary.
"""

from datetime import UTC, datetime, timedelta
import os
import uuid

import httpx
import jwt
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


@pytest.mark.asyncio
async def test_bearer_admin_acceptance_is_recovered_to_po_input(api_client):
    """A human acceptance is durable, credential-attributed and delivered by PO."""
    from src.tasks.owner_notifications import supervise_owed_owner_notifications

    project_id = str(uuid.uuid4())
    telegram_id = uuid.uuid4().int % 1_000_000_000
    other_telegram_id = uuid.uuid4().int % 1_000_000_000
    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    async with httpx.AsyncClient(
        base_url=api_client.base_url, headers=headers, timeout=30.0
    ) as client:
        user = await client.post(
            "/api/users/",
            json={
                "telegram_id": telegram_id,
                "username": f"acceptance_admin_{telegram_id}",
                "is_admin": True,
            },
        )
        assert user.status_code == httpx.codes.CREATED, user.text
        admin_id = user.json()["id"]
        other = await client.post(
            "/api/users/",
            json={
                "telegram_id": other_telegram_id,
                "username": f"acceptance_other_{other_telegram_id}",
            },
        )
        assert other.status_code == httpx.codes.CREATED, other.text
        other_id = other.json()["id"]
        project = await client.post(
            "/api/projects/",
            json={
                "id": project_id,
                "initiating_run_id": "test-run-1",
                "title": "Human acceptance recovery",
                "config": {},
            },
            headers={**headers, "X-Telegram-ID": str(telegram_id)},
        )
        assert project.status_code == httpx.codes.CREATED, project.text
        created = await client.post(
            "/api/stories/", json={"project_id": project_id, "title": "Ship accepted result"}
        )
        assert created.status_code == httpx.codes.CREATED, created.text
        story_id = created.json()["id"]
        assert (await client.post(f"/api/stories/{story_id}/start")).status_code == httpx.codes.OK
        assert (
            await client.post(f"/api/stories/{story_id}/human-review")
        ).status_code == httpx.codes.OK

        token = jwt.encode(
            {
                "sub": str(admin_id),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            "test-lk-jwt-secret",
            algorithm="HS256",
        )
        bearer_headers = {"Authorization": f"Bearer {token}"}
        non_admin_token = jwt.encode(
            {
                "sub": str(other_id),
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(hours=1),
            },
            "test-lk-jwt-secret",
            algorithm="HS256",
        )
        non_admin = await client.post(
            f"/api/stories/{story_id}/accept-result",
            json={"basis": "I am not authorized to accept this."},
            headers={"Authorization": f"Bearer {non_admin_token}"},
        )
        assert non_admin.status_code == httpx.codes.FORBIDDEN, non_admin.text
        mismatch = await client.post(
            f"/api/stories/{story_id}/accept-result",
            json={"basis": "Verified the deployment manually."},
            headers={**bearer_headers, "X-Telegram-ID": str(other_telegram_id)},
        )
        assert mismatch.status_code == httpx.codes.FORBIDDEN, mismatch.text
        missing_basis = await client.post(
            f"/api/stories/{story_id}/accept-result", json={}, headers=bearer_headers
        )
        assert missing_basis.status_code == httpx.codes.UNPROCESSABLE_ENTITY, missing_basis.text

        accepted = await client.post(
            f"/api/stories/{story_id}/accept-result",
            json={"basis": "Verified the deployment manually."},
            headers=bearer_headers,
        )
        assert accepted.status_code == httpx.codes.OK, accepted.text
        assert accepted.json()["status"] == "completed"
        acceptance = accepted.json()["operator_acceptance"]
        assert acceptance["actor"] == f"admin:{admin_id}"
        assert acceptance["basis"] == "Verified the deployment manually."
        assert acceptance["accepted_at"]
        assert accepted.json()["quarantine_reason"] is None
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

        settled = OwnerNotification.model_validate(
            (await api_client.request("GET", f"stories/{story_id}/owner-notification")).json()
        )
        assert settled.state is OwnerNotificationState.DELIVERED
    finally:
        await redis_client.close()


@pytest.mark.asyncio
async def test_admin_console_acceptance_is_recovered_to_po_input(api_client):
    """The nginx-authenticated console identity reaches the audited completion route."""
    from src.tasks.owner_notifications import supervise_owed_owner_notifications

    project_id = str(uuid.uuid4())
    telegram_id = uuid.uuid4().int % 1_000_000_000
    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    async with httpx.AsyncClient(
        base_url=api_client.base_url, headers=headers, timeout=30.0
    ) as client:
        user = await client.post(
            "/api/users/",
            json={"telegram_id": telegram_id, "username": f"console_owner_{telegram_id}"},
        )
        assert user.status_code == httpx.codes.CREATED, user.text
        project = await client.post(
            "/api/projects/",
            json={
                "id": project_id,
                "initiating_run_id": "test-run-1",
                "title": "Console acceptance recovery",
                "config": {},
            },
            headers={**headers, "X-Telegram-ID": str(telegram_id)},
        )
        assert project.status_code == httpx.codes.CREATED, project.text
        created = await client.post(
            "/api/stories/", json={"project_id": project_id, "title": "Ship reviewed result"}
        )
        assert created.status_code == httpx.codes.CREATED, created.text
        story_id = created.json()["id"]
        assert (await client.post(f"/api/stories/{story_id}/start")).status_code == httpx.codes.OK
        assert (
            await client.patch(
                f"/api/stories/{story_id}",
                json={"quarantine_reason": {"qa_failure": {"fingerprint": "false-blocker"}}},
            )
        ).status_code == httpx.codes.OK
        assert (
            await client.post(f"/api/stories/{story_id}/human-review")
        ).status_code == httpx.codes.OK

        accepted = await client.post(
            f"/api/stories/{story_id}/accept-result",
            json={"basis": "Verified the running deployment manually."},
            headers={"X-Admin-Console-Operator": "orchestrator-admin"},
        )
        assert accepted.status_code == httpx.codes.OK, accepted.text
        acceptance = accepted.json()["operator_acceptance"]
        assert acceptance["actor"] == "admin_console:orchestrator-admin"
        assert acceptance["overridden_quarantine_reason"] == {
            "qa_failure": {"fingerprint": "false-blocker"}
        }
        assert accepted.json()["quarantine_reason"] is None
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

        settled = OwnerNotification.model_validate(
            (await api_client.request("GET", f"stories/{story_id}/owner-notification")).json()
        )
        assert settled.state is OwnerNotificationState.DELIVERED
    finally:
        await redis_client.close()
