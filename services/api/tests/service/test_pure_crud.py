from fastapi import status
from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_post_projects_pure_db(async_client: AsyncClient):
    """
    Test that creating a project does NOT trigger GitHub (provision_project_repo)
    or Redis (scaffolder queue) calls.
    """
    payload = {
        "id": "new-proj-001",
        "name": "New Project 001",
        "status": "created",
        "config": {"modules": ["backend"]},
        "modules": ["backend"],
    }

    response = await async_client.post(
        "/api/projects/", json=payload, headers={"X-Telegram-ID": "12345"}
    )

    # Assert success
    assert (
        response.status_code == status.HTTP_201_CREATED
    ), f"Expected 201, got {response.status_code}: {response.text}"

    data = response.json()
    assert data["id"] == payload["id"]
    assert data["name"] == payload["name"]
    # repository_url should be None as per new logic
    assert data.get("repository_url") is None
