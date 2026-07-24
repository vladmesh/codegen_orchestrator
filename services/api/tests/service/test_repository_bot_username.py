"""bot_username lives on the repository row — QA reads it from there.

Token validation writes it once; every later read (deploy→QA handoff, admin
E2E trigger) goes through this column, so it has to survive the process that
wrote it.
"""

import uuid

from fastapi import status
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TELEGRAM_ID = "100685"


@pytest.fixture
async def bot_project(async_client: AsyncClient) -> tuple[str, str]:
    """A project with a primary repository, as create_project leaves it.

    Returns (project_id, repository_id). Each test gets its own project so the
    list-by-project read sees exactly one primary repository.
    """
    await async_client.post(
        "/api/users/",
        json={"telegram_id": int(TELEGRAM_ID), "username": "bot-username-tester"},
    )
    project_id = str(uuid.uuid4())
    project_resp = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": "Palindrome Bot",
            "status": "created",
            "config": {"modules": ["backend", "tg_bot"]},
        },
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert project_resp.status_code == status.HTTP_201_CREATED, project_resp.text

    repo_resp = await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": "palindrome-bot",
            "git_url": "pending://palindrome-bot",
        },
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert repo_resp.status_code == status.HTTP_201_CREATED, repo_resp.text
    assert repo_resp.json()["bot_username"] is None
    return project_id, repo_resp.json()["id"]


@pytest.mark.asyncio
async def test_bot_username_is_committed_to_the_row(
    async_client: AsyncClient, db_session: AsyncSession, bot_project: tuple[str, str]
):
    """PATCH writes through to Postgres, so a restarted service still sees it."""
    from shared.models.repository import Repository

    _, repository_id = bot_project

    patch_resp = await async_client.patch(
        f"/api/repositories/{repository_id}",
        json={"bot_username": "palindrome_bot"},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert patch_resp.status_code == status.HTTP_200_OK, patch_resp.text

    # Read the row on a connection that never saw the write.
    row = await db_session.scalar(select(Repository).where(Repository.id == repository_id))
    assert row.bot_username == "palindrome_bot"


@pytest.mark.asyncio
async def test_stored_username_is_readable_by_the_qa_producers(
    async_client: AsyncClient, bot_project: tuple[str, str]
):
    """The deploy→QA handoff lists repositories by project — it must be there."""
    project_id, repository_id = bot_project

    await async_client.patch(
        f"/api/repositories/{repository_id}",
        json={"bot_username": "palindrome_bot"},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )

    list_resp = await async_client.get(
        f"/api/repositories/?project_id={project_id}",
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert list_resp.status_code == status.HTTP_200_OK, list_resp.text
    primary = next(r for r in list_resp.json() if r["role"] == "primary")
    assert primary["bot_username"] == "palindrome_bot"

    get_resp = await async_client.get(
        f"/api/repositories/{repository_id}",
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert get_resp.json()["bot_username"] == "palindrome_bot"


@pytest.mark.asyncio
async def test_revalidating_a_token_replaces_the_username(
    async_client: AsyncClient, bot_project: tuple[str, str]
):
    """A user pointing the project at a different bot overwrites the old value."""
    _, repository_id = bot_project

    for username in ("first_bot", "second_bot"):
        await async_client.patch(
            f"/api/repositories/{repository_id}",
            json={"bot_username": username},
            headers={"X-Telegram-ID": TELEGRAM_ID},
        )

    get_resp = await async_client.get(
        f"/api/repositories/{repository_id}",
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert get_resp.json()["bot_username"] == "second_bot"


@pytest.mark.asyncio
async def test_other_fields_survive_a_bot_username_patch(
    async_client: AsyncClient, bot_project: tuple[str, str]
):
    """PATCH is partial — writing the username must not drop the QA criteria."""
    _, repository_id = bot_project

    before = await async_client.get(
        f"/api/repositories/{repository_id}", headers={"X-Telegram-ID": TELEGRAM_ID}
    )
    criteria = before.json()["acceptance_criteria"]
    assert criteria

    await async_client.patch(
        f"/api/repositories/{repository_id}",
        json={"bot_username": "palindrome_bot"},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )

    after = await async_client.get(
        f"/api/repositories/{repository_id}", headers={"X-Telegram-ID": TELEGRAM_ID}
    )
    assert after.json()["acceptance_criteria"] == criteria
    assert after.json()["git_url"] == "pending://palindrome-bot"
