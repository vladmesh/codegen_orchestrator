"""DTO contract tests — verify API responses validate against shared DTOs.

These tests catch the #1 cross-service failure mode: a field renamed/removed
in the API schema while the shared DTO (used by scheduler, langgraph, scaffolder,
infra-service) still expects the old shape.

Each test creates an entity via POST, reads it back, and validates the JSON
response against the corresponding DTO from shared/contracts/dto/.
"""

import uuid

from httpx import AsyncClient
import pytest

from shared.contracts.dto.application import ApplicationDTO
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.server import ServerDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO

TELEGRAM_ID = 999111999


@pytest.fixture
async def _test_user(async_client: AsyncClient):
    resp = await async_client.get(f"/api/users/by-telegram/{TELEGRAM_ID}")
    if resp.status_code == 404:
        resp = await async_client.post(
            "/api/users/",
            json={
                "telegram_id": TELEGRAM_ID,
                "username": "dto_contract",
                "first_name": "DTO",
                "is_admin": True,
            },
        )
    return resp.json()


@pytest.fixture
async def project(async_client: AsyncClient, _test_user):
    resp = await async_client.post(
        "/api/projects/",
        json={"name": "dto-contract-project", "status": "active", "config": {}},
        headers={"X-Telegram-ID": str(TELEGRAM_ID)},
    )
    assert resp.status_code == 201
    return resp.json()


# ── Project ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_response_validates_as_dto(project):
    dto = ProjectDTO.model_validate(project)
    assert dto.name == "dto-contract-project"
    assert dto.status == "active"
    assert dto.created_at is not None


# ── Task ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_response_validates_as_dto(async_client: AsyncClient, project):
    resp = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": project["id"],
            "title": "dto contract task",
            "status": "backlog",
            "priority": 1,
        },
    )
    assert resp.status_code == 201
    dto = TaskDTO.model_validate(resp.json())
    assert dto.title == "dto contract task"
    assert dto.status == "backlog"
    assert dto.created_at is not None


@pytest.mark.asyncio
async def test_task_event_response_validates_as_dto(async_client: AsyncClient, project):
    # Create a task and transition it to generate an event
    resp = await async_client.post(
        "/api/tasks/",
        json={
            "project_id": project["id"],
            "title": "dto event task",
            "status": "backlog",
            "priority": 0,
        },
    )
    task_id = resp.json()["id"]

    # Add a note event
    resp = await async_client.post(
        f"/api/tasks/{task_id}/events",
        json={"event_type": "note", "details": {"msg": "test"}, "actor": "test"},
    )
    assert resp.status_code == 201
    dto = TaskEventDTO.model_validate(resp.json())
    assert dto.event_type == "note"
    assert dto.actor == "test"


# ── Story ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_story_response_validates_as_dto(async_client: AsyncClient, project):
    resp = await async_client.post(
        "/api/stories/",
        json={
            "project_id": project["id"],
            "title": "dto contract story",
            "type": "product",
            "created_by": "test",
        },
    )
    assert resp.status_code == 201
    dto = StoryDTO.model_validate(resp.json())
    assert dto.title == "dto contract story"
    assert dto.type == "product"
    assert dto.status == "created"


# ── Repository ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repository_response_validates_as_dto(async_client: AsyncClient, project):
    resp = await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project["id"],
            "name": "dto-contract-repo",
            "git_url": f"https://github.com/test/dto-contract-{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 201
    dto = RepositoryDTO.model_validate(resp.json())
    assert dto.name == "dto-contract-repo"
    assert dto.role == "primary"
    assert dto.is_managed is True


# ── Server ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_response_validates_as_dto(async_client: AsyncClient):
    handle = f"dto-test-{uuid.uuid4().hex[:8]}"
    resp = await async_client.post(
        "/api/servers/",
        json={
            "handle": handle,
            "host": "dto-test.example.com",
            "public_ip": "10.0.0.99",
            "ssh_user": "dev",
            "is_managed": False,
            "status": "discovered",
            "labels": {},
        },
    )
    assert resp.status_code == 201
    dto = ServerDTO.model_validate(resp.json())
    assert dto.handle == handle
    assert dto.public_ip == "10.0.0.99"
    assert dto.ssh_user == "dev"
    assert dto.status == "discovered"
    assert dto.created_at is not None

    get_resp = await async_client.get(f"/api/servers/{handle}")
    assert get_resp.status_code == 200
    assert ServerDTO.model_validate(get_resp.json()).ssh_user == "dev"

    patch_resp = await async_client.patch(f"/api/servers/{handle}", json={"ssh_user": "runner"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["ssh_user"] == "runner"

    invalid_resp = await async_client.patch(
        f"/api/servers/{handle}", json={"ssh_user": "invalid user"}
    )
    assert invalid_resp.status_code == 422


# ── Application ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_application_response_validates_as_dto(
    async_client: AsyncClient,
    project,
):
    # Need a repo and server first
    repo_resp = await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project["id"],
            "name": "dto-app-repo",
            "git_url": f"https://github.com/test/dto-app-{uuid.uuid4().hex[:8]}",
        },
    )
    repo_id = repo_resp.json()["id"]

    server_handle = f"dto-app-srv-{uuid.uuid4().hex[:8]}"
    await async_client.post(
        "/api/servers/",
        json={
            "handle": server_handle,
            "host": "app.example.com",
            "public_ip": "10.0.0.100",
            "is_managed": False,
            "status": "active",
            "labels": {},
        },
    )

    resp = await async_client.post(
        "/api/applications/",
        json={
            "repo_id": repo_id,
            "server_handle": server_handle,
            "service_name": "backend",
        },
    )
    assert resp.status_code == 201
    dto = ApplicationDTO.model_validate(resp.json())
    assert dto.service_name == "backend"
    assert dto.status == "not_deployed"
    assert isinstance(dto.ports, list)
