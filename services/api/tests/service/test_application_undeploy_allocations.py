"""Per-application undeploy releases only that runtime's port reservations.

The deploy consumer reaches this API boundary only after remote SSH teardown
succeeds.  The application remains as deployment history, but its runtime port
reservations must not survive a terminal ``not_deployed`` status.
"""

from http import HTTPStatus
import uuid

from httpx import AsyncClient
import pytest

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus


async def _project_with_repo(client: AsyncClient) -> str:
    telegram_id = 860_000_000 + (uuid.uuid4().int % 100_000_000)
    created_user = await client.post(
        "/api/users/",
        json={"telegram_id": telegram_id, "username": f"undeploy-{telegram_id}"},
    )
    assert created_user.status_code == HTTPStatus.CREATED, created_user.text

    project_id = str(uuid.uuid4())
    created_project = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": f"Undeploy allocations {project_id[:8]}",
            "initiating_run_id": f"test-run-{project_id[:8]}",
            "status": ProjectStatus.ACTIVE.value,
            "config": {"modules": ["backend"]},
        },
        headers={"X-Telegram-ID": str(telegram_id)},
    )
    assert created_project.status_code == HTTPStatus.CREATED, created_project.text

    created_repo = await client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"undeploy-{project_id[:8]}",
            "git_url": f"pending://{project_id}",
        },
    )
    assert created_repo.status_code == HTTPStatus.CREATED, created_repo.text
    return created_repo.json()["id"]


async def _server(client: AsyncClient) -> str:
    handle = f"undeploy-{uuid.uuid4().hex[:12]}"
    created = await client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": f"{handle}.example.com",
            "public_ip": "10.0.0.21",
            "ssh_user": "root",
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return handle


async def _application(client: AsyncClient, repo_id: str, server_handle: str) -> int:
    created = await client.post(
        "/api/applications/",
        json={
            "repo_id": repo_id,
            "server_handle": server_handle,
            "service_name": f"svc-{uuid.uuid4().hex[:8]}",
            "status": ApplicationStatus.RUNNING.value,
        },
    )
    assert created.status_code == HTTPStatus.CREATED, created.text
    return created.json()["id"]


async def _allocate(
    client: AsyncClient, server_handle: str, application_id: int, service_name: str
) -> int:
    allocated = await client.post(
        f"/api/servers/{server_handle}/ports/allocate-next",
        json={"application_id": application_id, "service_name": service_name},
    )
    assert allocated.status_code == HTTPStatus.OK, allocated.text
    return allocated.json()["port"]


async def _allocations(client: AsyncClient, application_id: int) -> list[dict]:
    response = await client.get(f"/api/allocations/?application_id={application_id}")
    assert response.status_code == HTTPStatus.OK, response.text
    return response.json()


@pytest.mark.asyncio
async def test_undeploy_releases_only_owned_allocations_and_reuses_ports(async_client: AsyncClient):
    """A terminal undeploy frees every owned runtime port, atomically and idempotently."""
    server_handle = await _server(async_client)
    app_id = await _application(async_client, await _project_with_repo(async_client), server_handle)
    other_app_id = await _application(
        async_client, await _project_with_repo(async_client), server_handle
    )

    released_ports = [
        await _allocate(async_client, server_handle, app_id, service_name)
        for service_name in ("backend", "postgres", "redis")
    ]
    other_port = await _allocate(async_client, server_handle, other_app_id, "other-backend")

    undeployed = await async_client.patch(
        f"/api/applications/{app_id}",
        json={"status": ApplicationStatus.NOT_DEPLOYED.value},
    )
    assert undeployed.status_code == HTTPStatus.OK, undeployed.text
    assert undeployed.json()["status"] == ApplicationStatus.NOT_DEPLOYED.value
    assert await _allocations(async_client, app_id) == []
    assert [
        allocation["port"] for allocation in await _allocations(async_client, other_app_id)
    ] == [other_port]

    replay = await async_client.patch(
        f"/api/applications/{app_id}",
        json={"status": ApplicationStatus.NOT_DEPLOYED.value},
    )
    assert replay.status_code == HTTPStatus.OK, replay.text
    assert await _allocations(async_client, app_id) == []

    reused_port = await _allocate(async_client, server_handle, app_id, "backend")
    assert reused_port == min(released_ports)


@pytest.mark.asyncio
async def test_stopping_an_application_keeps_its_port_allocations(async_client: AsyncClient):
    """Stop is reversible, so it must retain the target's runtime reservations."""
    server_handle = await _server(async_client)
    app_id = await _application(async_client, await _project_with_repo(async_client), server_handle)
    allocated_port = await _allocate(async_client, server_handle, app_id, "backend")

    stopped = await async_client.patch(
        f"/api/applications/{app_id}",
        json={"status": ApplicationStatus.STOPPED.value},
    )
    assert stopped.status_code == HTTPStatus.OK, stopped.text
    assert [allocation["port"] for allocation in await _allocations(async_client, app_id)] == [
        allocated_port
    ]
