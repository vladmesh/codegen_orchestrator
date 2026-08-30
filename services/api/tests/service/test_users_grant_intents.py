"""Service-level durability and ownership checks for generated-service grants."""

from unittest.mock import AsyncMock
import uuid

import pytest

from shared.contracts.dto.users_grant import USERS_GRANT_INTENT_KEY, GrantIntentStatus


class _PublishRedis:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.publish_message = AsyncMock(side_effect=error)


async def _target(async_client):
    suffix = uuid.uuid4().hex[:10]
    owner_telegram_id = uuid.uuid4().int % 1_000_000_000
    incoming_telegram_id = uuid.uuid4().int % 1_000_000_000
    project_id = str(uuid.uuid4())
    server_handle = f"grant-{suffix}"

    owner = await async_client.post(
        "/api/users/",
        json={"telegram_id": owner_telegram_id, "username": f"owner_{suffix}"},
    )
    assert owner.status_code == 201
    incoming = await async_client.post(
        "/api/users/",
        json={"telegram_id": incoming_telegram_id, "username": f"incoming_{suffix}"},
    )
    assert incoming.status_code == 201
    project = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": f"Grant target {suffix}",
            "initiating_run_id": f"run-{suffix}",
            "config": {"modules": ["backend", "tg_bot"]},
        },
        headers={"X-Telegram-ID": str(owner_telegram_id)},
    )
    assert project.status_code == 201, project.text
    server = await async_client.post(
        "/api/servers/",
        json={
            "handle": server_handle,
            "host": f"{server_handle}.example.test",
            "public_ip": "10.0.0.1",
            "status": "active",
            "is_managed": True,
        },
    )
    assert server.status_code == 201, server.text
    repository = await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"grant-{suffix}",
            "git_url": f"https://example.test/{suffix}.git",
            "role": "primary",
        },
    )
    assert repository.status_code == 201, repository.text
    application = await async_client.post(
        "/api/applications/",
        json={
            "repo_id": repository.json()["id"],
            "server_handle": server_handle,
            "service_name": f"grant-{suffix}",
            "status": "running",
        },
    )
    assert application.status_code == 201, application.text
    sha = "a" * 40
    deployment = await async_client.post(
        "/api/service-deployments/",
        json={
            "application_id": application.json()["id"],
            "project_id": project_id,
            "service_name": f"grant-{suffix}",
            "server_handle": server_handle,
            "port": 8080,
            "result": "success",
            "deployed_sha": sha,
        },
    )
    assert deployment.status_code == 201, deployment.text
    return {
        "project_id": project_id,
        "owner": owner.json(),
        "incoming": incoming.json(),
        "application_id": application.json()["id"],
        "server_handle": server_handle,
        "service_name": f"grant-{suffix}",
        "sha": sha,
    }


@pytest.mark.asyncio
async def test_grant_intent_is_durable_before_a_publish_failure(async_client):
    from src.dependencies import get_redis_client
    from src.main import app

    target = await _target(async_client)
    redis = _PublishRedis(error=ConnectionError("redis unavailable"))
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        response = await async_client.post(
            f"/api/projects/{target['project_id']}/users/grant",
            json={"telegram_id": target["incoming"]["telegram_id"]},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)

    assert response.status_code == 503
    intent_id = (
        f"users-grant-add_user-{uuid.UUID(target['project_id']).hex}-"
        f"{target['incoming']['telegram_id']}"
    )
    stored = await async_client.get(f"/api/runs/{intent_id}")
    assert stored.status_code == 200
    intent = stored.json()["run_metadata"][USERS_GRANT_INTENT_KEY]
    assert intent["status"] == GrantIntentStatus.PUBLISH_OWED.value
    redis.publish_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_grant_repeat_reuses_its_intent_and_refuses_a_stale_target(async_client):
    from src.dependencies import get_redis_client
    from src.main import app

    target = await _target(async_client)
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{target['project_id']}/users/grant",
            json={"telegram_id": target["incoming"]["telegram_id"]},
        )
        second = await async_client.post(
            f"/api/projects/{target['project_id']}/users/grant",
            json={"telegram_id": target["incoming"]["telegram_id"]},
        )
        newer = await async_client.post(
            "/api/service-deployments/",
            json={
                "application_id": target["application_id"],
                "project_id": target["project_id"],
                "service_name": target["service_name"],
                "server_handle": target["server_handle"],
                "port": 8080,
                "result": "success",
                "deployed_sha": "b" * 40,
            },
        )
        assert newer.status_code == 201
        stale = await async_client.post(
            f"/api/projects/{target['project_id']}/users/grant",
            json={"telegram_id": target["incoming"]["telegram_id"]},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert second.status_code == 200
    assert second.json() == {**first.json(), "created": False}
    assert stale.status_code == 409
    run = await async_client.get(f"/api/runs/{first.json()['intent_id']}")
    intent = run.json()["run_metadata"][USERS_GRANT_INTENT_KEY]
    intent["status"] = GrantIntentStatus.APPLIED.value
    patched = await async_client.patch(
        f"/api/runs/{first.json()['intent_id']}",
        json={"run_metadata": {USERS_GRANT_INTENT_KEY: intent}},
    )
    assert patched.status_code == 200
    applied = await async_client.post(
        f"/api/projects/{target['project_id']}/users/grant",
        json={"telegram_id": target["incoming"]["telegram_id"]},
    )
    assert applied.status_code == 200
    assert applied.json()["status"] == GrantIntentStatus.APPLIED.value
    assert redis.publish_message.await_count == 2
    runs = await async_client.get("/api/runs/", params={"project_id": target["project_id"]})
    assert len([run for run in runs.json() if USERS_GRANT_INTENT_KEY in run["run_metadata"]]) == 1


@pytest.mark.asyncio
async def test_transfer_requires_worker_readback_and_keeps_the_race_guard(async_client):
    from src.dependencies import get_redis_client
    from src.main import app

    target = await _target(async_client)
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        staged = await async_client.post(
            f"/api/projects/{target['project_id']}/ownership-transfer",
            json={"telegram_id": target["incoming"]["telegram_id"]},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)

    assert staged.status_code == 200
    intent_id = staged.json()["intent_id"]
    unproven = await async_client.post(
        f"/api/projects/{target['project_id']}/ownership-transfer/{intent_id}/apply"
    )
    assert unproven.status_code == 409

    run = await async_client.get(f"/api/runs/{intent_id}")
    intent = run.json()["run_metadata"][USERS_GRANT_INTENT_KEY]
    intent["status"] = GrantIntentStatus.APPLYING.value
    marked = await async_client.patch(
        f"/api/runs/{intent_id}", json={"run_metadata": {USERS_GRANT_INTENT_KEY: intent}}
    )
    assert marked.status_code == 200
    applied = await async_client.post(
        f"/api/projects/{target['project_id']}/ownership-transfer/{intent_id}/apply"
    )
    assert applied.status_code == 200
    project = await async_client.get(f"/api/projects/{target['project_id']}")
    assert project.json()["owner_id"] == target["incoming"]["id"]
    repeated = await async_client.post(
        f"/api/projects/{target['project_id']}/ownership-transfer/{intent_id}/apply"
    )
    assert repeated.status_code == 200


@pytest.mark.asyncio
async def test_transfer_does_not_move_ownership_after_its_outgoing_owner_changed(async_client):
    from src.dependencies import get_redis_client
    from src.main import app

    target = await _target(async_client)
    replacement_telegram_id = uuid.uuid4().int % 1_000_000_000
    replacement = await async_client.post(
        "/api/users/",
        json={
            "telegram_id": replacement_telegram_id,
            "username": f"replacement_{uuid.uuid4().hex}",
        },
    )
    assert replacement.status_code == 201
    redis = _PublishRedis()
    app.dependency_overrides[get_redis_client] = lambda: redis
    try:
        first = await async_client.post(
            f"/api/projects/{target['project_id']}/ownership-transfer",
            json={"telegram_id": target["incoming"]["telegram_id"]},
        )
        replacement_transfer = await async_client.post(
            f"/api/projects/{target['project_id']}/ownership-transfer",
            json={"telegram_id": replacement_telegram_id},
        )
    finally:
        app.dependency_overrides.pop(get_redis_client, None)
    assert first.status_code == 200
    assert replacement_transfer.status_code == 200

    replacement_intent_id = replacement_transfer.json()["intent_id"]
    replacement_run = await async_client.get(f"/api/runs/{replacement_intent_id}")
    replacement_intent = replacement_run.json()["run_metadata"][USERS_GRANT_INTENT_KEY]
    replacement_intent["status"] = GrantIntentStatus.APPLYING.value
    assert (
        await async_client.patch(
            f"/api/runs/{replacement_intent_id}",
            json={"run_metadata": {USERS_GRANT_INTENT_KEY: replacement_intent}},
        )
    ).status_code == 200
    assert (
        await async_client.post(
            f"/api/projects/{target['project_id']}/ownership-transfer/{replacement_intent_id}/apply"
        )
    ).status_code == 200

    first_run = await async_client.get(f"/api/runs/{first.json()['intent_id']}")
    first_intent = first_run.json()["run_metadata"][USERS_GRANT_INTENT_KEY]
    first_intent["status"] = GrantIntentStatus.APPLYING.value
    assert (
        await async_client.patch(
            f"/api/runs/{first.json()['intent_id']}",
            json={"run_metadata": {USERS_GRANT_INTENT_KEY: first_intent}},
        )
    ).status_code == 200
    raced = await async_client.post(
        f"/api/projects/{target['project_id']}/ownership-transfer/{first.json()['intent_id']}/apply"
    )
    assert raced.status_code == 409
    project = await async_client.get(f"/api/projects/{target['project_id']}")
    assert project.json()["owner_id"] == replacement.json()["id"]


@pytest.mark.asyncio
async def test_only_the_deduplicated_initial_owner_seed_run_carries_an_owner_intent(async_client):
    target = await _target(async_client)
    owner_telegram_id = target["owner"]["telegram_id"]
    seed_id = f"users-grant-initial-owner-{uuid.UUID(target['project_id']).hex}-{owner_telegram_id}"
    seed = await async_client.post(
        "/api/runs/",
        json={
            "id": seed_id,
            "type": "deploy",
            "project_id": target["project_id"],
            "run_metadata": {
                "head_sha": target["sha"],
                "triggered_by": "initial_owner_seed",
            },
        },
    )
    assert seed.status_code == 201, seed.text
    intent = seed.json()["run_metadata"][USERS_GRANT_INTENT_KEY]
    assert intent["id"] == seed_id
    assert intent["kind"] == "initial_owner"

    machinery = await async_client.post(
        "/api/runs/",
        json={
            "id": f"temporary-access-revoke-{uuid.uuid4().hex}",
            "type": "deploy",
            "project_id": target["project_id"],
            "run_metadata": {
                "head_sha": target["sha"],
                "triggered_by": "temporary_access_revoke",
            },
        },
    )
    assert machinery.status_code == 201, machinery.text
    assert USERS_GRANT_INTENT_KEY not in machinery.json()["run_metadata"]
