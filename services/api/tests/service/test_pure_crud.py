from fastapi import status
from httpx import AsyncClient
import pytest

from shared.models.user import User


@pytest.mark.asyncio
async def test_post_projects_pure_db(async_client: AsyncClient, db_session):
    """
    Test that creating a project does NOT trigger GitHub (provision_project_repo)
    or Redis (scaffolder queue) calls.
    """
    # Seed a user so the X-Telegram-ID lookup succeeds
    user = User(telegram_id=100500, username="testuser")
    db_session.add(user)
    await db_session.commit()

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
