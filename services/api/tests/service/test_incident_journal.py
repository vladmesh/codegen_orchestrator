"""Service coverage for the provisioning incident journal."""

import asyncio
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_provisioning_failure_reuses_active_incident_and_starts_new_resolved_episode(
    async_client,
):
    handle = f"incident-journal-{uuid4().hex}"
    response = await async_client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": "test.example.com",
            "public_ip": "10.0.0.99",
            "status": "active",
            "is_managed": True,
        },
    )
    assert response.status_code == 201

    async def record(step: str):
        return await async_client.post(
            "/api/incidents/provisioning-failure",
            json={
                "server_handle": handle,
                "incident_type": "provisioning_failed",
                "details": {"step": step},
            },
        )

    first, repeated = await asyncio.gather(record("access_setup"), record("software_setup"))
    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json()["id"] == repeated.json()["id"]

    active = await async_client.get(
        "/api/incidents/",
        params={
            "server_handle": handle,
            "incident_type": "provisioning_failed",
            "status": "detected",
        },
    )
    assert active.status_code == 200
    assert len(active.json()) == 1
    assert active.json()[0]["recovery_attempts"] == 1
    assert active.json()[0]["details"]["step"] in {"access_setup", "software_setup"}

    incident_id = active.json()[0]["id"]
    resolved = await async_client.patch(
        f"/api/incidents/{incident_id}",
        json={"status": "resolved", "resolved_at": "2026-07-13T00:00:00Z"},
    )
    assert resolved.status_code == 200

    new_episode = await record("reinstall")
    assert new_episode.status_code == 200
    assert new_episode.json()["id"] != incident_id
    assert new_episode.json()["recovery_attempts"] == 0


@pytest.mark.asyncio
async def test_provisioning_failure_without_handle_is_rejected(async_client):
    # A NULL server_handle does not collide in the active unique index, so the
    # route would open a fresh episode on every retry instead of deduplicating.
    response = await async_client.post(
        "/api/incidents/provisioning-failure",
        json={"incident_type": "provisioning_failed", "details": {"step": "access_setup"}},
    )
    assert response.status_code == 422

    active = await async_client.get(
        "/api/incidents/", params={"incident_type": "provisioning_failed", "status": "detected"}
    )
    assert active.status_code == 200
    assert all(item["server_handle"] is not None for item in active.json())


@pytest.mark.asyncio
async def test_platform_incident_is_created_without_handle(async_client):
    response = await async_client.post(
        "/api/incidents/",
        json={
            "incident_type": "provider_api_unavailable",
            "details": {"dependency": "time4vps_api", "status_code": 401},
        },
    )
    assert response.status_code == 201
    assert response.json()["server_handle"] is None


@pytest.mark.asyncio
async def test_database_rejects_server_bound_incident_without_handle(db_session):
    # Defense in depth: a writer bypassing the request schema still cannot store
    # a server-bound incident with a NULL handle.
    from sqlalchemy.exc import IntegrityError

    from shared.models import Incident

    db_session.add(Incident(server_handle=None, incident_type="provisioning_failed"))
    with pytest.raises(IntegrityError):
        await db_session.flush()
