"""Service coverage for the durable intent lifecycle, separate from Runs."""

from unittest.mock import AsyncMock
import uuid

import pytest

from shared.contracts.dto.users_grant import GrantIntentStatus


class _PublishRedis:
    def __init__(self, error: Exception | None = None) -> None:
        self.publish_message = AsyncMock(side_effect=error)


async def _target(client):
    suffix = uuid.uuid4().hex[:8]
    project_id = str(uuid.uuid4())
    owner_id, user_id = (
        uuid.uuid4().int % 1_000_000_000,
        uuid.uuid4().int % 1_000_000_000,
    )
    owner = await client.post(
        "/api/users/", json={"telegram_id": owner_id, "username": f"o{suffix}"}
    )
    user = await client.post("/api/users/", json={"telegram_id": user_id, "username": f"u{suffix}"})
    project = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": f"grant {suffix}",
            "initiating_run_id": f"run-{suffix}",
            "config": {"modules": ["backend", "tg_bot"]},
        },
        headers={"X-Telegram-ID": str(owner_id)},
    )
    server = await client.post(
        "/api/servers/",
        json={
            "handle": f"grant-{suffix}",
            "host": f"grant-{suffix}.test",
            "public_ip": "10.0.0.1",
            "status": "active",
            "is_managed": True,
        },
    )
    repo = await client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"grant-{suffix}",
            "git_url": f"https://example.test/{suffix}.git",
            "role": "primary",
        },
    )
    app = await client.post(
        "/api/applications/",
        json={
            "repo_id": repo.json()["id"],
            "server_handle": server.json()["handle"],
            "service_name": f"grant-{suffix}",
            "status": "running",
        },
    )
    sha = "a" * 40
    deployment = await client.post(
        "/api/service-deployments/",
        json={
            "application_id": app.json()["id"],
            "project_id": project_id,
            "service_name": f"grant-{suffix}",
            "server_handle": server.json()["handle"],
            "port": 8080,
            "result": "success",
            "deployed_sha": sha,
        },
    )
    for response in (owner, user, project, server, repo, app, deployment):
        assert response.status_code in {200, 201}, response.text
    return project_id, owner.json(), user.json(), app.json(), server.json(), sha


@pytest.mark.asyncio
async def test_intent_is_durable_before_publish_and_not_a_run(async_client):
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, user, _, _, _ = await _target(async_client)
    redis = _PublishRedis(ConnectionError("down"))
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        response = await async_client.post(
            f"/api/projects/{project_id}/users/grant", json={"telegram_id": user["telegram_id"]}
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
    assert response.status_code == 503
    intent_id = (
        response.json()["detail"]
        if False
        else (f"users-grant-add_user-{uuid.UUID(project_id).hex}-{user['telegram_id']}")
    )
    intent = await async_client.get(f"/api/projects/{project_id}/users/grant-intents/{intent_id}")
    assert intent.json()["status"] == GrantIntentStatus.PUBLISH_OWED.value
    assert intent.json()["execution_run_id"].startswith("deploy-grant-")
    deploy_message = redis.publish_message.await_args.args[1]
    assert deploy_message.story_id == ""


@pytest.mark.asyncio
async def test_retryable_stale_intent_rebinds_and_applied_redelivery_stops(async_client):
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, user, application, server, sha = await _target(async_client)
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{project_id}/users/grant", json={"telegram_id": user["telegram_id"]}
        )
        intent_id = first.json()["intent_id"]
        failed = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{intent_id}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": False},
        )
        assert failed.json()["status"] == GrantIntentStatus.RETRYABLE.value
        await async_client.post(
            "/api/service-deployments/",
            json={
                "application_id": application["id"],
                "project_id": project_id,
                "service_name": application["service_name"],
                "server_handle": server["handle"],
                "port": 8080,
                "result": "success",
                "deployed_sha": "b" * 40,
            },
        )
        rebound = await async_client.post(
            f"/api/projects/{project_id}/users/grant", json={"telegram_id": user["telegram_id"]}
        )
        assert rebound.json()["execution_run_id"] != first.json()["execution_run_id"]
        applied = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{intent_id}/complete",
            json={"execution_run_id": rebound.json()["execution_run_id"], "active": True},
        )
        assert applied.json()["status"] == GrantIntentStatus.APPLIED.value
        repeated = await async_client.post(
            f"/api/projects/{project_id}/users/grant", json={"telegram_id": user["telegram_id"]}
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
    # An applied intent is durable history, not this call's deploy attempt.
    # A caller must not mistake the old attempt id for a newly dispatched run.
    assert repeated.json()["disposition"] == "already_applied"
    assert repeated.json()["execution_run_id"] is None
    intent = await async_client.get(f"/api/projects/{project_id}/users/grant-intents/{intent_id}")
    assert intent.json()["target_sha"] == "b" * 40
    assert intent.json()["target_history"][0]["sha"] == sha
    assert redis.publish_message.await_count == 2


@pytest.mark.asyncio
async def test_initial_owner_lifecycle_returns_only_the_run_dispatched_by_this_call(async_client):
    """An APPLIED seed never lends its old run to a later merged PR."""
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, _, _, _, sha = await _target(async_client)
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
        assert first.json()["disposition"] == "dispatched"
        assert first.json()["execution_run_id"].startswith("deploy-grant-")
        intent_id = first.json()["intent_id"]
        applied = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{intent_id}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": True},
        )
        assert applied.json()["status"] == GrantIntentStatus.APPLIED.value
        repeated = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": "b" * 40},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)

    assert repeated.json()["disposition"] == "already_applied"
    assert repeated.json()["execution_run_id"] is None


@pytest.mark.asyncio
async def test_transfer_is_atomic_with_active_readback(async_client):
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, owner, incoming, _, _, _ = await _target(async_client)
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        staged = await async_client.post(
            f"/api/projects/{project_id}/ownership-transfer",
            json={"telegram_id": incoming["telegram_id"]},
        )
        completed = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{staged.json()['intent_id']}/complete",
            json={"execution_run_id": staged.json()["execution_run_id"], "active": True},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
    assert completed.status_code == 200
    project = await async_client.get(f"/api/projects/{project_id}")
    assert project.json()["owner_id"] == incoming["id"]
    assert project.json()["owner_id"] != owner["id"]
