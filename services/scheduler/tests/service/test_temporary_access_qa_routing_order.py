"""Real API contract: the access sweep may run before or after story QA routing.

A QA run passed, and its temporary access grant has run out of revoke attempts.
Whichever of `supervise_temporary_access` and `supervise_testing_stories` runs
first, the story ends completed on its passed verdict and the cleanup incident is
recorded once, after routing. The dispatcher's call order is not what holds this.
"""

import os
import uuid

import httpx
import pytest

from shared.contracts.dto.qa_handoff import (
    QA_DISPATCHED_AT_KEY,
    QA_HANDOFF_KEY,
    QAHandoffPlan,
    TemporaryAccessRequest,
)
from shared.contracts.dto.temporary_access import TemporaryAccessRevokeReason, TemporaryAccessStatus
from shared.contracts.queues.qa import QAMessage
from shared.redis import RedisStreamClient
from shared.tests.ssh_key_fixtures import fleet_private_key
from src.tasks.supervisor import supervise_testing_stories
from src.tasks.temporary_access import _max_revoke_attempts, supervise_temporary_access

HEAD_SHA = "c" * 40
BUILT_SHA = "d" * 40
TARGET_URL = "https://routing-order.example.com"


class _Admins:
    """Records administrator alerts, keyed by the grant they name."""

    def __init__(self) -> None:
        self.grants: list[str | None] = []

    async def __call__(self, message: str, **fields: object) -> None:
        self.grants.append(fields.get("grant_id"))


@pytest.fixture
async def redis_client():
    client = RedisStreamClient(os.environ["REDIS_URL"])
    await client.connect()
    try:
        yield client
    finally:
        await client.close()


async def _call(api_client, method: str, path: str, **kwargs) -> httpx.Response:
    return await api_client.request_raw(method, path, **kwargs)


async def _passed_story_with_exhausted_grant(api_client) -> tuple[str, str, str]:
    """A TESTING story whose QA passed, and a grant whose last revoke attempt failed."""
    suffix = uuid.uuid4().hex[:8]
    telegram_id = uuid.uuid4().int % 1_000_000_000
    user = await _call(
        api_client,
        "POST",
        "users/",
        json={"telegram_id": telegram_id, "username": f"order-{telegram_id}"},
    )
    assert user.is_success, user.text
    project_id = str(uuid.uuid4())
    project = await _call(
        api_client,
        "POST",
        "projects/",
        json={
            "id": project_id,
            "title": "Routing order",
            "initiating_run_id": f"init-{suffix}",
            "status": "active",
            "config": {"workspace_ready": True},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert project.is_success, project.text
    server = await _call(
        api_client,
        "POST",
        "servers/",
        json={
            "handle": f"order-{suffix}",
            "host": "h.test",
            "public_ip": "10.9.0.11",
            "ssh_key": fleet_private_key(),
        },
    )
    assert server.status_code == httpx.codes.CREATED, server.text
    repository = await _call(
        api_client,
        "POST",
        "repositories/",
        json={
            "project_id": project_id,
            "name": f"order-{suffix}",
            "git_url": f"https://github.com/test/order-{suffix}.git",
        },
    )
    assert repository.status_code == httpx.codes.CREATED, repository.text
    application = await _call(
        api_client,
        "POST",
        "applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": f"order-{suffix}",
            "status": "running",
        },
    )
    assert application.status_code == httpx.codes.CREATED, application.text
    application_id = application.json()["id"]
    deployment = await _call(
        api_client,
        "POST",
        "service-deployments/",
        json={
            "application_id": application_id,
            "project_id": project_id,
            "service_name": f"order-{suffix}",
            "server_handle": server.json()["handle"],
            "port": 8080,
            "result": "success",
            "deployed_sha": HEAD_SHA,
            "deployment_info": {"deployed_commit_sha": BUILT_SHA},
        },
    )
    assert deployment.status_code == httpx.codes.CREATED, deployment.text

    story = await _call(
        api_client, "POST", "stories/", json={"project_id": project_id, "title": "Order"}
    )
    assert story.status_code == httpx.codes.CREATED, story.text
    story_id = story.json()["id"]
    for action in ("start", "deploy", "test"):
        moved = await _call(api_client, "POST", f"stories/{story_id}/{action}")
        assert moved.is_success, moved.text

    qa_run_id = f"qa-order-{suffix}"
    # The deploy handoff's plan, already dispatched: completing the story on a
    # passed verdict reads the deployed address from it.
    handoff = QAHandoffPlan(
        qa_message=QAMessage(
            story_id=story_id,
            project_id=project_id,
            initiating_run_id=f"deploy-{qa_run_id}",
            deployed_url=TARGET_URL,
            application_id=application_id,
            acceptance_criteria="the bot answers /start",
            run_id=qa_run_id,
        ),
        access=TemporaryAccessRequest(
            target_application_id=application_id,
            target_base_url=TARGET_URL,
            head_sha=HEAD_SHA,
        ),
    ).model_dump(mode="json")
    qa = await _call(
        api_client,
        "POST",
        "work-admission/paid-runs",
        json={
            "id": qa_run_id,
            "type": "qa",
            "project_id": project_id,
            "story_id": story_id,
            "run_metadata": {
                "application_id": application_id,
                QA_HANDOFF_KEY: handoff,
                QA_DISPATCHED_AT_KEY: "2026-09-22T00:00:00+00:00",
            },
        },
    )
    assert qa.status_code == httpx.codes.OK, qa.text
    assert qa.json()["run_id"] == qa_run_id

    grant_id = f"tempaccess-{qa_run_id}"
    created = await _call(
        api_client,
        "POST",
        "temporary-access-grants/",
        json={
            "id": grant_id,
            "project_id": project_id,
            "channel": "telegram",
            "external_id": "8202532144",
            "target_application_id": application_id,
            "target_base_url": TARGET_URL,
            "head_sha": HEAD_SHA,
            "qa_run_id": qa_run_id,
            "grant_run_id": f"temporary-access-grant-{suffix}",
            "qa_message": {
                "project_id": project_id,
                "initiating_run_id": f"deploy-{qa_run_id}",
                "telegram_chat_id": "",
                "deployed_url": TARGET_URL,
                "application_id": application_id,
                "acceptance_criteria": "the bot answers /start",
                "run_id": qa_run_id,
            },
        },
    )
    assert created.status_code == httpx.codes.CREATED, created.text

    passed = await _call(
        api_client,
        "PATCH",
        f"runs/{qa_run_id}",
        json={"status": "completed", "result": {"qa_outcome": "passed", "summary": "all green"}},
    )
    assert passed.is_success, passed.text

    revoke_run_id = f"temporary-access-revoke-{suffix}"
    exhausted = await _call(
        api_client,
        "PATCH",
        f"temporary-access-grants/{grant_id}",
        json={
            "status": TemporaryAccessStatus.REVOKING.value,
            "revoke_reason": TemporaryAccessRevokeReason.RUN_TERMINAL.value,
            "revoke_run_id": revoke_run_id,
            "revoke_attempts": _max_revoke_attempts(),
        },
    )
    assert exhausted.is_success, exhausted.text
    await _fail_revoke_run(api_client, project_id, revoke_run_id)
    return story_id, qa_run_id, grant_id


async def _fail_revoke_run(api_client, project_id: str, run_id: str) -> None:
    """The capability revoke ran and could not read the access back inactive."""
    existing = await _call(api_client, "GET", f"runs/{run_id}")
    if existing.status_code == httpx.codes.NOT_FOUND:
        created = await _call(
            api_client,
            "POST",
            "runs/",
            json={"id": run_id, "type": "deploy", "project_id": project_id},
        )
        assert created.status_code == httpx.codes.CREATED, created.text
    failed = await _call(
        api_client,
        "PATCH",
        f"runs/{run_id}",
        json={
            "status": "failed",
            "error_message": "access still active",
            "result": {"deploy_outcome": "give_up", "error_details": "access still active"},
        },
    )
    assert failed.is_success, failed.text


async def _grant(api_client, grant_id: str) -> dict:
    response = await _call(api_client, "GET", f"temporary-access-grants/{grant_id}")
    assert response.is_success, response.text
    return response.json()


async def _assert_story_kept_its_pass(api_client, story_id: str, qa_run_id: str) -> None:
    story = (await _call(api_client, "GET", f"stories/{story_id}")).json()
    assert story["status"] == "completed"
    assert story["quarantine_reason"] is None
    run = (await _call(api_client, "GET", f"runs/{qa_run_id}")).json()
    assert run["status"] == "completed"
    assert run["result"]["qa_outcome"] == "passed"
    # The routing transition recorded that this story consumed this run.
    assert run["qa_routed_at"] is not None


@pytest.mark.asyncio
async def test_sweep_before_routing_defers_the_incident_until_the_story_routed(
    api_client, redis_client, monkeypatch
):
    admins = _Admins()
    monkeypatch.setattr("src.tasks.temporary_access.notify_admins_best_effort", admins)
    story_id, qa_run_id, grant_id = await _passed_story_with_exhausted_grant(api_client)
    project_id = (await _grant(api_client, grant_id))["project_id"]

    await supervise_temporary_access(api_client, redis_client)

    grant = await _grant(api_client, grant_id)
    assert grant["escalated_at"] is None
    assert grant_id not in admins.grants
    # Cleanup goes on, and does not spend another attempt doing so.
    assert grant["status"] == TemporaryAccessStatus.REVOKING.value
    assert grant["revoke_attempts"] == _max_revoke_attempts()
    story = (await _call(api_client, "GET", f"stories/{story_id}")).json()
    assert story["status"] == "testing"
    assert story["quarantine_reason"] is None

    await supervise_testing_stories(api_client, redis_client)
    await _assert_story_kept_its_pass(api_client, story_id, qa_run_id)

    # The redispatched revoke fails too; now the incident is recorded, once.
    await _fail_revoke_run(api_client, project_id, grant["revoke_run_id"])
    await supervise_temporary_access(api_client, redis_client)
    await supervise_temporary_access(api_client, redis_client)

    grant = await _grant(api_client, grant_id)
    assert grant["escalated_at"] is not None
    assert grant["status"] == TemporaryAccessStatus.REVOKE_FAILED.value
    assert admins.grants.count(grant_id) == 1
    await _assert_story_kept_its_pass(api_client, story_id, qa_run_id)


@pytest.mark.asyncio
async def test_routing_before_sweep_records_the_incident_on_the_first_sweep(
    api_client, redis_client, monkeypatch
):
    admins = _Admins()
    monkeypatch.setattr("src.tasks.temporary_access.notify_admins_best_effort", admins)
    story_id, qa_run_id, grant_id = await _passed_story_with_exhausted_grant(api_client)

    await supervise_testing_stories(api_client, redis_client)
    await _assert_story_kept_its_pass(api_client, story_id, qa_run_id)

    await supervise_temporary_access(api_client, redis_client)
    await supervise_temporary_access(api_client, redis_client)

    grant = await _grant(api_client, grant_id)
    assert grant["escalated_at"] is not None
    assert grant["status"] == TemporaryAccessStatus.REVOKE_FAILED.value
    assert admins.grants.count(grant_id) == 1
    await _assert_story_kept_its_pass(api_client, story_id, qa_run_id)
