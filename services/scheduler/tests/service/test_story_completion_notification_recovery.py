"""Completion and notification recovery through the real API and Redis."""

from datetime import UTC, datetime, timedelta
import os
import uuid

import httpx
import jwt
import pytest

from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.qa_handoff import QA_HANDOFF_KEY, QAHandoffPlan
from shared.contracts.queues.po import POSystemEvent, from_flat_fields
from shared.contracts.queues.qa import QAMessage
from shared.queues import PO_INPUT_QUEUE
from shared.redis_client import RedisStreamClient


async def _record_running_acceptance_target(
    client: httpx.AsyncClient, project_id: str, story_id: str
) -> None:
    """Give an acceptance scenario the current reachable QA handoff it needs."""
    server = await client.post(
        "/api/servers/",
        json={
            "handle": f"acceptance-{uuid.uuid4().hex[:8]}",
            "host": "acceptance.test",
            "public_ip": "10.8.0.9",
            "ssh_user": "root",
        },
    )
    assert server.status_code == httpx.codes.CREATED, server.text
    repository = await client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"acceptance-{uuid.uuid4().hex[:8]}",
            "git_url": "https://github.com/test/acceptance.git",
        },
    )
    assert repository.status_code == httpx.codes.CREATED, repository.text
    application = await client.post(
        "/api/applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": "acceptance-service",
            "status": "running",
        },
    )
    assert application.status_code == httpx.codes.CREATED, application.text
    application_id = application.json()["id"]
    qa_run_id = f"qa-acceptance-{uuid.uuid4().hex[:12]}"
    qa_run = await client.post(
        "/api/work-admission/paid-runs",
        json={
            "id": qa_run_id,
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {
                QA_HANDOFF_KEY: QAHandoffPlan(
                    qa_message=QAMessage(
                        project_id=project_id,
                        initiating_run_id="test-run-1",
                        deployed_url="http://10.8.0.9:8000",
                        application_id=application_id,
                        acceptance_criteria="the service responds",
                        run_id=qa_run_id,
                    )
                ).model_dump(mode="json"),
            },
        },
    )
    assert qa_run.status_code == httpx.codes.OK, qa_run.text
    terminal = await client.patch(
        f"/api/runs/{qa_run_id}",
        json={"status": "completed", "result": {"qa_outcome": "failed"}},
    )
    assert terminal.status_code == httpx.codes.OK, terminal.text


@pytest.mark.asyncio
async def test_direct_completion_without_qa_is_recovered_to_po_input(api_client):
    """Direct completion retains its durable PO-notification recovery path."""
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

        # This API action creates the durable record before returning, even
        # without a QA handoff, so scheduler recovery can deliver it later.
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
        await _record_running_acceptance_target(client, project_id, story_id)

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
        await _record_running_acceptance_target(client, project_id, story_id)

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


@pytest.mark.asyncio
async def test_recheck_qa_restores_a_quarantined_story_through_completion(  # noqa: PLR0915
    api_client,
):
    """A repaired QA infrastructure blocker needs no hand transition to finish."""
    from src.tasks.supervisor import supervise_deploying_stories, supervise_testing_stories

    project_id = str(uuid.uuid4())
    telegram_id = uuid.uuid4().int % 1_000_000_000
    headers = {"X-Internal-Key": os.environ["INTERNAL_API_KEY"]}
    head_sha = "0123456789abcdef0123456789abcdef01234567"
    async with httpx.AsyncClient(
        base_url=api_client.base_url, headers=headers, timeout=30.0
    ) as client:
        user = await client.post(
            "/api/users/",
            json={"telegram_id": telegram_id, "username": f"recheck_owner_{telegram_id}"},
        )
        assert user.status_code == httpx.codes.CREATED, user.text
        project = await client.post(
            "/api/projects/",
            json={
                "id": project_id,
                "initiating_run_id": "recheck-e2e-init",
                "title": "Recheck QA completion",
                "config": {},
            },
            headers={**headers, "X-Telegram-ID": str(telegram_id)},
        )
        assert project.status_code == httpx.codes.CREATED, project.text
        server = await client.post(
            "/api/servers/",
            json={
                "handle": f"recheck-e2e-{uuid.uuid4().hex[:8]}",
                "host": "recheck-e2e.test",
                "public_ip": "10.2.0.9",
                "ssh_user": "root",
            },
        )
        assert server.status_code == httpx.codes.CREATED, server.text
        repository = await client.post(
            "/api/repositories/",
            json={
                "project_id": project_id,
                "name": "recheck-e2e-repository",
                "git_url": "https://github.com/test/recheck-e2e-repository.git",
            },
        )
        assert repository.status_code == httpx.codes.CREATED, repository.text
        application = await client.post(
            "/api/applications/",
            json={
                "repo_id": repository.json()["id"],
                "server_handle": server.json()["handle"],
                "service_name": "recheck-e2e-service",
                "status": "stopped",
            },
        )
        assert application.status_code == httpx.codes.CREATED, application.text
        application_id = application.json()["id"]
        allocated = await client.post(
            f"/api/servers/{server.json()['handle']}/ports/allocate-next",
            json={"service_name": "recheck-e2e-service", "application_id": application_id},
        )
        assert allocated.status_code == httpx.codes.OK, allocated.text

        story = await client.post(
            "/api/stories/", json={"project_id": project_id, "title": "Recheck E2E"}
        )
        assert story.status_code == httpx.codes.CREATED, story.text
        story_id = story.json()["id"]
        assert (await client.post(f"/api/stories/{story_id}/start")).status_code == httpx.codes.OK
        assert (
            await client.post(f"/api/stories/{story_id}/human-review")
        ).status_code == httpx.codes.OK
        receipt = await client.post(
            "/api/runs/",
            json={
                "id": f"deploy-receipt-{uuid.uuid4().hex[:12]}",
                "type": "deploy",
                "project_id": project_id,
                "story_id": story_id,
                "run_metadata": {"application_id": application_id, "head_sha": head_sha},
            },
        )
        assert receipt.status_code == httpx.codes.CREATED, receipt.text
        blocker = {
            "qa_outcome": "blocked",
            "blocker": {
                "category": "server_unavailable",
                "attempted": "connect QA probe",
                "sent": "GET /health",
                "received": "connection refused",
            },
        }
        quarantined_qa = await client.post(
            "/api/work-admission/paid-runs",
            json={
                "id": f"qa-quarantine-{uuid.uuid4().hex[:12]}",
                "type": "qa",
                "project_id": project_id,
                "story_id": story_id,
                "run_metadata": {"application_id": application_id},
            },
        )
        assert quarantined_qa.status_code == httpx.codes.OK, quarantined_qa.text
        assert (quarantined_qa_id := quarantined_qa.json()["run_id"])
        terminal = await client.patch(
            f"/api/runs/{quarantined_qa_id}",
            json={"status": "failed", "result": blocker},
        )
        assert terminal.status_code == httpx.codes.OK, terminal.text
        assert (
            await client.patch(f"/api/stories/{story_id}", json={"quarantine_reason": blocker})
        ).status_code == httpx.codes.OK

        rechecked = await client.post(
            f"/api/stories/{story_id}/recheck-qa",
            json={"basis": "The server was repaired."},
            headers={**headers, "X-Admin-Console-Operator": "shared-console"},
        )
        assert rechecked.status_code == httpx.codes.OK, rechecked.text
        recheck_run_id = rechecked.json()["operator_recheck"]["run_id"]
        assert rechecked.json()["status"] == "deploying"

        deployed = await client.patch(
            f"/api/runs/{recheck_run_id}",
            json={
                "status": "completed",
                "result": {
                    "deploy_outcome": "success",
                    "deployed_url": "http://10.2.0.9:8000",
                    "application_id": application_id,
                },
            },
        )
        assert deployed.status_code == httpx.codes.OK, deployed.text
        stopped = await client.patch(
            f"/api/applications/{application_id}", json={"status": "stopped"}
        )
        assert stopped.status_code == httpx.codes.OK, stopped.text

        redis_client = RedisStreamClient(os.environ["REDIS_URL"])
        await redis_client.connect()
        try:
            deployed_counts = await supervise_deploying_stories(api_client, redis_client)
            assert deployed_counts["tested"] >= 1
            qa_runs = await client.get(
                "/api/runs/", params={"story_id": story_id, "run_type": "qa"}
            )
            assert qa_runs.status_code == httpx.codes.OK, qa_runs.text
            recheck_qa = next(run for run in qa_runs.json() if run["id"] != quarantined_qa_id)
            assert recheck_qa["story_id"] == story_id
            assert recheck_qa["run_metadata"]["application_id"] == application_id
            assert recheck_qa["run_metadata"]["deploy_run_id"] == recheck_run_id

            passed = await client.patch(
                f"/api/runs/{recheck_qa['id']}",
                json={
                    "status": "completed",
                    "result": {
                        "qa_outcome": "passed",
                        "deployed_url": "http://10.2.0.9:8000",
                    },
                },
            )
            assert passed.status_code == httpx.codes.OK, passed.text
            completed_counts = await supervise_testing_stories(api_client, redis_client)
            assert completed_counts["completed"] >= 1
        finally:
            await redis_client.close()

        completed = await client.get(f"/api/stories/{story_id}")
        assert completed.status_code == httpx.codes.OK, completed.text
        assert completed.json()["status"] == "completed"
        assert completed.json()["quarantine_reason"] is None
        notification = OwnerNotification.model_validate(
            (await client.get(f"/api/stories/{story_id}/owner-notification")).json()
        )
        assert "http://10.2.0.9:8000" in notification.text
        project_stories = await client.get("/api/stories/", params={"project_id": project_id})
        assert project_stories.status_code == httpx.codes.OK, project_stories.text
        assert [(story["id"], story["status"]) for story in project_stories.json()] == [
            (story_id, "completed")
        ]
