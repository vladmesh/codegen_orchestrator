"""The per-application monitoring switch changes monitoring, and nothing else.

Muting an application must not pass for an undeploy: its status, its port
allocations and its open SERVICE_DOWN incident all stay as they were. Only the
monitoring fields and an audit marker on the application's own incident change.
"""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest
from test_application_undeploy_allocations import (
    _allocate,
    _allocations,
    _application,
    _project_with_repo,
    _server,
)

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.incident import IncidentStatus, IncidentType


async def _service_down(client: AsyncClient, server_handle: str, application_id: int) -> int:
    created = await client.post(
        "/api/incidents/",
        json={
            "server_handle": server_handle,
            "incident_type": IncidentType.SERVICE_DOWN.value,
            "details": {"application_id": application_id, "consecutive_failures": 3},
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return created.json()["id"]


@pytest.mark.asyncio
async def test_disabling_monitoring_keeps_status_allocations_and_incident(
    async_client: AsyncClient,
):
    server_handle = await _server(async_client)
    app_id = await _application(async_client, await _project_with_repo(async_client), server_handle)
    sibling_id = await _application(
        async_client, await _project_with_repo(async_client), server_handle
    )
    await async_client.patch(
        f"/api/applications/{app_id}", json={"status": ApplicationStatus.DOWN.value}
    )
    port = await _allocate(async_client, server_handle, app_id, "backend")
    own_incident = await _service_down(async_client, server_handle, app_id)
    sibling_incident = await _service_down(async_client, server_handle, sibling_id)

    created = await async_client.get(f"/api/applications/{app_id}")
    assert created.json()["monitoring_enabled"] is True

    muted = await async_client.post(
        f"/api/applications/{app_id}/monitoring",
        json={"enabled": False, "reason": "bot is being reworked"},
    )
    assert muted.status_code == HTTPStatus.OK, muted.text
    body = muted.json()
    assert body["monitoring_enabled"] is False
    assert body["monitoring_changed_by"] == "internal_service"
    assert body["monitoring_changed_at"] is not None
    assert body["status"] == ApplicationStatus.DOWN.value
    assert [a["port"] for a in await _allocations(async_client, app_id)] == [port]

    incident = (await async_client.get(f"/api/incidents/{own_incident}")).json()
    assert incident["status"] == IncidentStatus.DETECTED.value
    assert incident["resolved_at"] is None
    assert incident["details"]["monitoring_muted"] is True
    assert incident["details"]["monitoring_events"][0]["reason"] == "bot is being reworked"

    sibling = (await async_client.get(f"/api/incidents/{sibling_incident}")).json()
    assert "monitoring_muted" not in sibling["details"]
    assert (await async_client.get(f"/api/applications/{sibling_id}")).json()[
        "monitoring_enabled"
    ] is True

    replay = await async_client.post(
        f"/api/applications/{app_id}/monitoring", json={"enabled": False}
    )
    assert replay.status_code == HTTPStatus.OK
    assert replay.json()["monitoring_changed_at"] == body["monitoring_changed_at"]

    resumed = await async_client.post(
        f"/api/applications/{app_id}/monitoring", json={"enabled": True}
    )
    assert resumed.status_code == HTTPStatus.OK, resumed.text
    assert resumed.json()["monitoring_enabled"] is True
    incident = (await async_client.get(f"/api/incidents/{own_incident}")).json()
    assert incident["details"]["monitoring_muted"] is False
    assert [e["action"] for e in incident["details"]["monitoring_events"]] == [
        "monitoring_disabled",
        "monitoring_enabled",
    ]


@pytest.mark.asyncio
async def test_monitoring_switch_for_unknown_application_is_404(async_client: AsyncClient):
    response = await async_client.post(
        "/api/applications/987654321/monitoring", json={"enabled": False}
    )
    assert response.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.asyncio
async def test_monitoring_switch_rejects_anonymous_caller(async_client: AsyncClient):
    response = await async_client.post(
        "/api/applications/1/monitoring",
        json={"enabled": False},
        headers={"X-Internal-Key": "wrong"},
    )
    assert response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)


async def _user(client: AsyncClient, *, is_admin: bool) -> tuple[int, int]:
    telegram_id = 870_000_000 + (uuid.uuid4().int % 100_000_000)
    created = await client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"mon-{telegram_id}", "is_admin": is_admin},
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return telegram_id, created.json()["id"]


@pytest.mark.asyncio
async def test_monitoring_switch_is_admin_only(async_client: AsyncClient):
    server_handle = await _server(async_client)
    app_id = await _application(async_client, await _project_with_repo(async_client), server_handle)
    user_tid, _ = await _user(async_client, is_admin=False)
    admin_tid, admin_id = await _user(async_client, is_admin=True)

    refused = await async_client.post(
        f"/api/applications/{app_id}/monitoring",
        json={"enabled": False},
        headers={"X-Telegram-ID": str(user_tid)},
    )
    assert refused.status_code == HTTPStatus.FORBIDDEN, refused.text
    assert (await async_client.get(f"/api/applications/{app_id}")).json()[
        "monitoring_enabled"
    ] is True

    accepted = await async_client.post(
        f"/api/applications/{app_id}/monitoring",
        json={"enabled": False},
        headers={"X-Telegram-ID": str(admin_tid)},
    )
    assert accepted.status_code == HTTPStatus.OK, accepted.text
    assert accepted.json()["monitoring_changed_by"] == f"admin:{admin_id}"

    console = await async_client.post(
        f"/api/applications/{app_id}/monitoring",
        json={"enabled": True},
        headers={"X-Admin-Console-Operator": "vlad"},
    )
    assert console.status_code == HTTPStatus.OK, console.text
    assert console.json()["monitoring_changed_by"] == "admin_console:vlad"


@pytest.mark.asyncio
async def test_guarded_incident_writes_are_refused_for_a_muted_application(
    async_client: AsyncClient,
):
    """The prober's create/resolve carry `if_monitored`; a muted app gets 409, others pass."""
    server_handle = await _server(async_client)
    app_id = await _application(async_client, await _project_with_repo(async_client), server_handle)
    sibling_id = await _application(
        async_client, await _project_with_repo(async_client), server_handle
    )
    open_incident = await _service_down(async_client, server_handle, app_id)
    await async_client.post(f"/api/applications/{app_id}/monitoring", json={"enabled": False})

    def payload(application_id: int) -> dict:
        return {
            "server_handle": server_handle,
            "incident_type": IncidentType.SERVICE_DOWN.value,
            "details": {"application_id": application_id},
        }

    refused = await async_client.post(
        "/api/incidents/?if_monitored=true&monitoring_generation=initial", json=payload(app_id)
    )
    assert refused.status_code == HTTPStatus.CONFLICT, refused.text
    refused = await async_client.patch(
        f"/api/incidents/{open_incident}?if_monitored=true&monitoring_generation=initial",
        json={"status": IncidentStatus.RESOLVED.value},
    )
    assert refused.status_code == HTTPStatus.CONFLICT, refused.text
    still_open = (await async_client.get(f"/api/incidents/{open_incident}")).json()
    assert still_open["status"] == IncidentStatus.DETECTED.value

    allowed = await async_client.post(
        "/api/incidents/?if_monitored=true&monitoring_generation=initial", json=payload(sibling_id)
    )
    assert allowed.status_code == HTTPStatus.CREATED, allowed.text
    unguarded = await async_client.post("/api/incidents/", json=payload(app_id))
    assert unguarded.status_code == HTTPStatus.CREATED, unguarded.text

    reenabled = await async_client.post(
        f"/api/applications/{app_id}/monitoring", json={"enabled": True}
    )
    generation = reenabled.json()["monitoring_changed_at"]

    # A probe that began before the off/on switch carries the old generation.
    stale = await async_client.patch(
        f"/api/incidents/{open_incident}",
        params={"if_monitored": "true", "monitoring_generation": "initial"},
        json={"status": IncidentStatus.RESOLVED.value},
    )
    assert stale.status_code == HTTPStatus.CONFLICT, stale.text

    resolved = await async_client.patch(
        f"/api/incidents/{open_incident}",
        params={"if_monitored": "true", "monitoring_generation": generation},
        json={"status": IncidentStatus.RESOLVED.value},
    )
    assert resolved.status_code == HTTPStatus.OK, resolved.text
