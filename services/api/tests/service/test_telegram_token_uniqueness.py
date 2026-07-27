"""One live project per bot, and the refusal says only what the asker may know.

Two projects on one bot used to be caught by Telegram itself, as a 409 somewhere in
deploy. Here the binding endpoint refuses on the way in, against a real database:
the same project re-binding is an iteration, the user's other project is named so
they can go there, and someone else's project is never described at all.
"""

from unittest.mock import patch
import uuid

from fastapi import status
import httpx
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.telegram import TokenRejectionReason, TokenVerdictStatus
from shared.crypto import decrypt_dict
from shared.models import Project, Repository

OWNER_ID = "100711"
OTHER_OWNER_ID = "100712"
TOKEN = "987654321:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105

# Captured before patching: the factory below must not call the patched name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture
def bot() -> str:
    """A bot nobody else is on: the service database outlives a single test."""
    return f"bot_{uuid.uuid4().hex[:8]}"


def _patched_telegram(username: str):
    """Telegram accepts the token, and nothing external is running on it."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": username}})
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"Unexpected Telegram method: {method}")

    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return patch("src.utils.telegram_token.httpx.AsyncClient", factory)


async def _make_project(client: AsyncClient, telegram_id: str, title: str) -> str:
    """A project with a primary repository, owned by `telegram_id`."""
    await client.post(
        "/api/users/",
        json={"telegram_id": int(telegram_id), "username": f"user-{telegram_id}"},
    )
    project_id = str(uuid.uuid4())
    resp = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": title,
            "status": ProjectStatus.ACTIVE.value,
            "config": {"modules": ["backend", "tg_bot"]},
        },
        headers={"X-Telegram-ID": telegram_id},
    )
    assert resp.status_code == status.HTTP_201_CREATED, resp.text

    repo_resp = await client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": title.lower().replace(" ", "-"),
            "git_url": f"pending://{project_id}",
        },
        headers={"X-Telegram-ID": telegram_id},
    )
    assert repo_resp.status_code == status.HTTP_201_CREATED, repo_resp.text
    return project_id


async def _bind(client: AsyncClient, project_id: str, telegram_id: str, bot: str) -> dict:
    with _patched_telegram(bot):
        resp = await client.post(
            f"/api/projects/{project_id}/telegram/token",
            json={"token": TOKEN},
            headers={"X-Telegram-ID": telegram_id},
        )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    return resp.json()


async def _stored_secrets(db_session: AsyncSession, project_id: str) -> dict:
    project = await db_session.scalar(select(Project).where(Project.id == uuid.UUID(project_id)))
    secrets = (project.config or {}).get("secrets") or {}
    return decrypt_dict(secrets) if secrets else {}


@pytest.mark.asyncio
async def test_rebinding_the_same_token_to_the_same_project_is_not_a_conflict(
    async_client: AsyncClient, db_session: AsyncSession, bot: str
):
    """The user re-sends the token to the project already holding it — an iteration."""
    project_id = await _make_project(async_client, OWNER_ID, "Palindrome")
    assert (await _bind(async_client, project_id, OWNER_ID, bot))["status"] == TokenVerdictStatus.OK

    verdict = await _bind(async_client, project_id, OWNER_ID, bot)

    assert verdict["status"] == TokenVerdictStatus.OK.value
    assert verdict["reason_code"] is None
    assert verdict["bot_username"] == bot
    assert (await _stored_secrets(db_session, project_id))["TELEGRAM_BOT_TOKEN"] == TOKEN


@pytest.mark.asyncio
async def test_own_other_project_is_named_so_the_user_can_go_there(
    async_client: AsyncClient, db_session: AsyncSession, bot: str
):
    holder_id = await _make_project(async_client, OWNER_ID, "Palindrome")
    await _bind(async_client, holder_id, OWNER_ID, bot)
    second_id = await _make_project(async_client, OWNER_ID, "Echo")

    verdict = await _bind(async_client, second_id, OWNER_ID, bot)

    assert verdict["status"] == TokenVerdictStatus.REJECTED.value
    assert verdict["reason_code"] == TokenRejectionReason.BOUND_TO_OWN_PROJECT.value
    assert "Palindrome" in verdict["user_message"]
    assert verdict["conflict_project_id"] == holder_id
    assert TOKEN not in verdict["user_message"]

    # Nothing was written to the project that asked.
    assert await _stored_secrets(db_session, second_id) == {}
    repo = await db_session.scalar(
        select(Repository).where(Repository.project_id == uuid.UUID(second_id))
    )
    assert repo.bot_username is None


@pytest.mark.asyncio
async def test_another_users_project_is_refused_without_leaking_anything(
    async_client: AsyncClient, db_session: AsyncSession, bot: str
):
    holder_id = await _make_project(async_client, OTHER_OWNER_ID, "Somebody Elses Bot")
    await _bind(async_client, holder_id, OTHER_OWNER_ID, bot)
    mine_id = await _make_project(async_client, OWNER_ID, "Echo")

    verdict = await _bind(async_client, mine_id, OWNER_ID, bot)

    assert verdict["status"] == TokenVerdictStatus.REJECTED.value
    assert verdict["reason_code"] == TokenRejectionReason.BOUND_ELSEWHERE.value

    # Not the project, not its id, not its owner — the whole payload, not just the message.
    body = str(verdict)
    assert "Somebody Elses Bot" not in body
    assert holder_id not in body
    assert OTHER_OWNER_ID not in body
    assert verdict["conflict_project_id"] is None
    assert TOKEN not in body

    assert await _stored_secrets(db_session, mine_id) == {}


@pytest.mark.asyncio
async def test_a_foreign_holder_outranks_the_users_own_project(
    async_client: AsyncClient, db_session: AsyncSession, bot: str
):
    """Two projects already on the bot, as before this check existed.

    Pointing the user at their own project would send them into the clash the other
    owner is already in, so the generic refusal wins.
    """
    own_id = await _make_project(async_client, OWNER_ID, "Palindrome")
    await _bind(async_client, own_id, OWNER_ID, bot)

    # The double binding predates the check, so it is written past the endpoint.
    foreign_id = await _make_project(async_client, OTHER_OWNER_ID, "Somebody Elses Bot")
    foreign_repo = await db_session.scalar(
        select(Repository).where(Repository.project_id == uuid.UUID(foreign_id))
    )
    foreign_repo.bot_username = bot
    await db_session.commit()

    third_id = await _make_project(async_client, OWNER_ID, "Echo")
    verdict = await _bind(async_client, third_id, OWNER_ID, bot)

    assert verdict["reason_code"] == TokenRejectionReason.BOUND_ELSEWHERE.value
    assert "Palindrome" not in verdict["user_message"]


@pytest.mark.asyncio
async def test_an_archived_project_lets_the_bot_go(
    async_client: AsyncClient, db_session: AsyncSession, bot: str
):
    holder_id = await _make_project(async_client, OWNER_ID, "Palindrome")
    await _bind(async_client, holder_id, OWNER_ID, bot)

    archived = await async_client.patch(
        f"/api/projects/{holder_id}",
        json={"status": ProjectStatus.ARCHIVED.value},
        headers={"X-Telegram-ID": OWNER_ID},
    )
    assert archived.status_code == status.HTTP_200_OK, archived.text

    second_id = await _make_project(async_client, OWNER_ID, "Echo")
    verdict = await _bind(async_client, second_id, OWNER_ID, bot)

    assert verdict["status"] == TokenVerdictStatus.OK.value
    assert (await _stored_secrets(db_session, second_id))["TELEGRAM_BOT_TOKEN"] == TOKEN
