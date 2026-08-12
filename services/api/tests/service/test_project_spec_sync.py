"""The project spec that github_sync writes has to survive the round trip.

github_sync reads `.project-spec.yaml` out of the repo and PATCHes it onto the
project; the architect reads it back off the project. Both halves run against a
real API and a real database here, because a request schema that merely forgets
the field fails as a 422 on the wire and nowhere else.
"""

import uuid

from httpx import AsyncClient
import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectUpdate

TELEGRAM_ID = 999222999

SPEC = {
    "version": "1.0",
    "name": "spec-sync",
    "services": [{"name": "backend", "port": 8000}],
}


@pytest.fixture
async def _spec_user(async_client: AsyncClient):
    resp = await async_client.get(f"/api/users/by-telegram/{TELEGRAM_ID}")
    if resp.status_code == 404:
        resp = await async_client.post(
            "/api/users/",
            json={
                "telegram_id": TELEGRAM_ID,
                "username": "spec_sync",
                "first_name": "Spec",
                "is_admin": True,
            },
        )
    return resp.json()


@pytest.fixture
async def project(async_client: AsyncClient, _spec_user) -> dict:
    resp = await async_client.post(
        "/api/projects/",
        json={
            "id": str(uuid.uuid4()),
            "title": "Spec Sync Project",
            "status": "active",
            "config": {"modules": ["backend"]},
        },
        headers={"X-Telegram-ID": str(TELEGRAM_ID)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_patch_project_spec_is_stored_and_read_back(async_client: AsyncClient, project: dict):
    """The github_sync call itself: PATCH with project_spec, then read it back."""
    body = ProjectUpdate(project_spec=SPEC).model_dump(exclude_unset=True, mode="json")

    patch = await async_client.patch(f"/api/projects/{project['id']}", json=body)

    assert patch.status_code == 200, patch.text
    assert patch.json()["project_spec"] == SPEC

    read = await async_client.get(f"/api/projects/{project['id']}")
    assert read.status_code == 200, read.text
    assert read.json()["project_spec"] == SPEC

    # github_sync parses every project response through this DTO.
    assert ProjectDTO.model_validate(read.json()).project_spec == SPEC


@pytest.mark.asyncio
async def test_patch_project_spec_leaves_config_alone(async_client: AsyncClient, project: dict):
    """A spec sync must not clobber the config the PO agent wrote at creation."""
    resp = await async_client.patch(f"/api/projects/{project['id']}", json={"project_spec": SPEC})

    assert resp.status_code == 200, resp.text
    assert resp.json()["config"] == {"modules": ["backend"], "agent_type": "claude"}


@pytest.mark.asyncio
async def test_project_create_rejects_fields_the_model_cannot_store(
    async_client: AsyncClient, _spec_user
):
    """`description` and `modules` belong in config; top level is not silently dropped."""
    resp = await async_client.post(
        "/api/projects/",
        json={
            "title": "Unstorable Fields",
            "description": "nowhere to put this",
            "modules": ["backend"],
        },
        headers={"X-Telegram-ID": str(TELEGRAM_ID)},
    )

    assert resp.status_code == 422, resp.text
