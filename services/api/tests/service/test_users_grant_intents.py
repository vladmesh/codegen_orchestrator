"""Service coverage for the durable intent lifecycle, separate from Runs."""

from unittest.mock import AsyncMock
import uuid

import pytest

from shared.contracts.dto.users_grant import GrantIntentStatus


class _PublishRedis:
    def __init__(self, error: Exception | None = None) -> None:
        self.publish_message = AsyncMock(side_effect=error)


@pytest.fixture(autouse=True)
async def _grant_retry_ceiling(async_client):
    """Keep the required lifecycle control available across this test module."""
    payload = {"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"}
    response = await async_client.post("/api/system-configs/", json=payload)
    assert response.status_code == 201
    yield
    response = await async_client.post("/api/system-configs/", json=payload)
    assert response.status_code == 201


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
async def test_initial_owner_lifecycle_stops_at_the_configured_deploy_retry_ceiling(async_client):
    """A terminal readback cannot mint or publish the (max + 1) grant Run."""
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, _, _, _, sha = await _target(async_client)
    prior = await async_client.get("/api/system-configs/deploy.max_deploy_retries")
    assert prior.status_code in {200, 404}
    configured = await async_client.post(
        "/api/system-configs/",
        json={
            "key": "deploy.max_deploy_retries",
            "value": 2,
            "category": "deploy",
        },
    )
    assert configured.status_code == 201

    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
        assert first.json()["disposition"] == "dispatched"
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{first.json()['execution_run_id']}", json={"status": "failed"}
        )
        second = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
        assert second.json()["disposition"] == "dispatched"
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{second.json()['intent_id']}/complete",
            json={"execution_run_id": second.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{second.json()['execution_run_id']}", json={"status": "failed"}
        )
        exhausted = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
        if prior.status_code == 200:
            await async_client.post("/api/system-configs/", json=prior.json())
        else:
            await async_client.delete("/api/system-configs/deploy.max_deploy_retries")

    assert exhausted.json()["disposition"] == "exhausted"
    assert exhausted.json()["status"] == GrantIntentStatus.FAILED.value
    assert exhausted.json()["execution_run_id"] is None
    assert redis.publish_message.await_count == 2
    persisted = await async_client.get(
        f"/api/projects/{project_id}/users/grant-intents/{exhausted.json()['intent_id']}"
    )
    assert persisted.json()["status"] == GrantIntentStatus.FAILED.value
    assert persisted.json()["detail"] == "deployment retry ceiling exhausted"
    assert persisted.json()["attempts"] == 2


@pytest.mark.asyncio
async def test_zero_retry_ceiling_persists_created_terminal_intent(async_client):
    """A max=0 admission records the created terminal intent before returning."""
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, _, _, _, sha = await _target(async_client)
    configured = await async_client.post(
        "/api/system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 0, "category": "deploy"},
    )
    assert configured.status_code == 201
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        exhausted = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
        await async_client.post(
            "/api/system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
        )

    assert exhausted.json()["disposition"] == "exhausted"
    intent = await async_client.get(
        f"/api/projects/{project_id}/users/grant-intents/{exhausted.json()['intent_id']}"
    )
    assert intent.json()["status"] == GrantIntentStatus.FAILED.value
    assert intent.json()["attempts"] == 0
    redis.publish_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_rebind_starts_a_fresh_bounded_retry_epoch(async_client):
    """A replacement target does not inherit a terminal target's admissions."""
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, _, _, _, sha = await _target(async_client)
    configured = await async_client.post(
        "/api/system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
    )
    assert configured.status_code == 201
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{first.json()['execution_run_id']}", json={"status": "failed"}
        )
        rebound = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": "b" * 40},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
        await async_client.post(
            "/api/system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
        )

    assert rebound.json()["disposition"] == "dispatched"
    assert rebound.json()["execution_run_id"] != first.json()["execution_run_id"]
    intent = await async_client.get(
        f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}"
    )
    assert intent.json()["attempts"] == 1
    history = intent.json()["target_history"]
    assert len(history) == 1
    assert history[0]["application_id"] is None
    assert history[0]["deployment_id"] is None
    assert history[0]["sha"] == sha
    assert history[0]["attempts"] == 1


@pytest.mark.asyncio
async def test_automatic_newer_seed_does_not_replace_a_live_execution(async_client):
    """A poller redelivery cannot orphan the current immutable grant Run."""
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
        newer = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": "b" * 40},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)

    assert newer.json()["disposition"] == "in_flight"
    assert newer.json()["execution_run_id"] is None
    intent = await async_client.get(
        f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}"
    )
    assert intent.json()["target_sha"] == sha
    assert intent.json()["target_history"] == []
    assert redis.publish_message.await_count == 1


@pytest.mark.asyncio
async def test_automatic_stale_recovery_after_replacement_returns_no_dispatch(async_client):
    """A supervisor retry for a superseded SHA leaves the replacement bound."""
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
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{first.json()['execution_run_id']}", json={"status": "failed"}
        )
        replacement = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": "b" * 40},
        )
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{replacement.json()['intent_id']}/complete",
            json={"execution_run_id": replacement.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{replacement.json()['execution_run_id']}", json={"status": "failed"}
        )
        stale = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)

    assert stale.json()["disposition"] == "stale_target"
    assert stale.json()["execution_run_id"] is None
    intent = await async_client.get(
        f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}"
    )
    assert intent.json()["target_sha"] == "b" * 40
    assert [target["sha"] for target in intent.json()["target_history"]] == [sha]
    assert redis.publish_message.await_count == 2


@pytest.mark.asyncio
async def test_automatic_backwards_target_history_rebind_is_refused(async_client):
    """A historic target is never rebound, even after its replacement is terminal."""
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
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{first.json()['execution_run_id']}", json={"status": "failed"}
        )
        replacement = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": "b" * 40},
        )
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{replacement.json()['intent_id']}/complete",
            json={"execution_run_id": replacement.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{replacement.json()['execution_run_id']}", json={"status": "failed"}
        )
        refused = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)

    assert refused.json()["disposition"] == "stale_target"
    intent = await async_client.get(
        f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}"
    )
    assert intent.json()["target_sha"] == "b" * 40
    assert len(intent.json()["target_history"]) == 1
    assert redis.publish_message.await_count == 2


@pytest.mark.asyncio
async def test_alternating_automatic_stale_shas_terminate_without_extra_publish(async_client):
    """Stale recoveries cannot reset the next target epoch into a ping-pong loop."""
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, _, _, _, sha = await _target(async_client)
    configured = await async_client.post(
        "/api/system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
    )
    assert configured.status_code == 201
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{first.json()['execution_run_id']}", json={"status": "failed"}
        )
        second = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": "b" * 40},
        )
        stale_while_live = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{second.json()['intent_id']}/complete",
            json={"execution_run_id": second.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{second.json()['execution_run_id']}", json={"status": "failed"}
        )
        exhausted = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": "b" * 40},
        )
        stale_after_terminal = await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/lifecycle",
            json={"kind": "initial_owner", "head_sha": sha},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
        await async_client.post(
            "/api/system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
        )

    assert stale_while_live.json()["disposition"] == "in_flight"
    assert exhausted.json()["disposition"] == "exhausted"
    assert stale_after_terminal.json()["disposition"] == "exhausted"
    assert redis.publish_message.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["users/grant", "ownership-transfer"])
async def test_explicit_permanent_access_retry_reopens_only_the_same_exhausted_intent(
    async_client, route
):
    """Manual retry opens a fresh epoch without duplicating the durable request."""
    from src.dependencies import get_redis_client
    from src.main import app

    project_id, _, user, _, _, _ = await _target(async_client)
    configured = await async_client.post(
        "/api/system-configs/",
        json={"key": "deploy.max_deploy_retries", "value": 1, "category": "deploy"},
    )
    assert configured.status_code == 201
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{project_id}/{route}", json={"telegram_id": user["telegram_id"]}
        )
        await async_client.post(
            f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}/complete",
            json={"execution_run_id": first.json()["execution_run_id"], "active": False},
        )
        await async_client.patch(
            f"/api/runs/{first.json()['execution_run_id']}", json={"status": "failed"}
        )
        exhausted = await async_client.post(
            f"/api/projects/{project_id}/{route}", json={"telegram_id": user["telegram_id"]}
        )
        retried = await async_client.post(
            f"/api/projects/{project_id}/{route}", json={"telegram_id": user["telegram_id"]}
        )
        duplicate = await async_client.post(
            f"/api/projects/{project_id}/{route}", json={"telegram_id": user["telegram_id"]}
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
        await async_client.post(
            "/api/system-configs/",
            json={"key": "deploy.max_deploy_retries", "value": 3, "category": "deploy"},
        )

    assert exhausted.json()["disposition"] == "exhausted"
    assert retried.json()["disposition"] == "dispatched"
    assert retried.json()["intent_id"] == first.json()["intent_id"]
    assert retried.json()["execution_run_id"] != first.json()["execution_run_id"]
    assert duplicate.json()["disposition"] == "in_flight"
    assert duplicate.json()["intent_id"] == first.json()["intent_id"]
    intent = await async_client.get(
        f"/api/projects/{project_id}/users/grant-intents/{first.json()['intent_id']}"
    )
    assert intent.json()["attempts"] == 1
    history = intent.json()["retry_history"]
    assert len(history) == 1
    assert history[0]["sha"] == intent.json()["target_sha"]
    assert history[0]["attempts"] == 1
    assert history[0]["reason"] == "explicit_retry"
    assert redis.publish_message.await_count == 2


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
