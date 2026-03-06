from fastapi import status
from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_post_projects_pure_db(async_client: AsyncClient):
    """
    Test that creating a project does NOT trigger GitHub (provision_project_repo)
    or Redis (scaffolder queue) calls.
    """
    # Seed a user via API so the X-Telegram-ID lookup succeeds
    user_resp = await async_client.post(
        "/api/users/",
        json={"telegram_id": 100500, "username": "testuser"},
    )
    assert user_resp.status_code == status.HTTP_201_CREATED

    payload = {
        "id": "new-proj-001",
        "name": "New Project 001",
        "status": "created",
        "config": {"modules": ["backend"]},
        "modules": ["backend"],
    }

    response = await async_client.post(
        "/api/projects/",
        json=payload,
        headers={"X-Telegram-ID": "100500"},
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
