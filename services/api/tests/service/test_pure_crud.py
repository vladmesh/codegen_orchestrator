import uuid

from fastapi import status
from httpx import AsyncClient
import pytest

PROJECT_UUID = str(uuid.UUID("00000000-0000-0000-0000-000000000099"))


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
        "id": PROJECT_UUID,
        "title": "New Project 001",
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
    assert response.status_code == status.HTTP_201_CREATED, (
        f"Expected 201, got {response.status_code}: {response.text}"
    )

    data = response.json()
    assert data["id"] == payload["id"]
    assert data["title"] == payload["title"]
    assert data["slug"] == "new-project-001-0000"
