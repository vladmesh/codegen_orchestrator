"""Binding a Telegram bot token goes through the validator endpoint, or not at all.

Covers the whole server-side path against a real database: a valid token lands in
the project's secrets and on the repository row, a rejected one changes nothing,
and the generic secrets endpoint refuses to take a token behind the validator's back.
"""

from unittest.mock import patch
import uuid

from fastapi import status
import httpx
from httpx import AsyncClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.telegram import TokenRejectionReason, TokenVerdictStatus
from shared.crypto import decrypt_dict
from shared.models import Project, Repository

TELEGRAM_ID = "100686"
VALID_TOKEN = "987654321:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105


# Captured before patching: the factory below must not call the patched name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched_telegram(handler):
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return patch("src.utils.telegram_token.httpx.AsyncClient", factory)


def _getme_ok(username: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"username": username}})

    return handler


def _getme_unauthorized(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})


@pytest.fixture
async def bot_project(async_client: AsyncClient) -> str:
    """Project with a primary repository, as create_project leaves it."""
    await async_client.post(
        "/api/users/",
        json={"telegram_id": int(TELEGRAM_ID), "username": "token-binding-tester"},
    )
    project_id = str(uuid.uuid4())
    project_resp = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": "Token Binding Bot",
            "status": "draft",
            "config": {"modules": ["backend", "tg_bot"]},
        },
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert project_resp.status_code == status.HTTP_201_CREATED, project_resp.text

    repo_resp = await async_client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": "token-binding-bot",
            "git_url": f"pending://{project_id}",
        },
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert repo_resp.status_code == status.HTTP_201_CREATED, repo_resp.text
    return project_id


async def _stored_secrets(db_session: AsyncSession, project_id: str) -> dict:
    project = await db_session.scalar(select(Project).where(Project.id == uuid.UUID(project_id)))
    secrets = (project.config or {}).get("secrets") or {}
    return decrypt_dict(secrets) if secrets else {}


@pytest.mark.asyncio
async def test_valid_token_is_stored_and_bot_username_lands_on_the_repository(
    async_client: AsyncClient, db_session: AsyncSession, bot_project: str
):
    with _patched_telegram(_getme_ok("token_binding_bot")):
        resp = await async_client.post(
            f"/api/projects/{bot_project}/telegram/token",
            json={"token": VALID_TOKEN},
            headers={"X-Telegram-ID": TELEGRAM_ID},
        )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    verdict = resp.json()
    assert verdict["status"] == TokenVerdictStatus.OK.value
    assert verdict["bot_username"] == "token_binding_bot"
    assert verdict["reason_code"] is None
    assert VALID_TOKEN not in verdict["user_message"]

    secrets = await _stored_secrets(db_session, bot_project)
    assert secrets["TELEGRAM_BOT_TOKEN"] == VALID_TOKEN
    assert secrets["TELEGRAM_BOT_USERNAME"] == "token_binding_bot"

    repo = await db_session.scalar(
        select(Repository).where(Repository.project_id == uuid.UUID(bot_project))
    )
    assert repo.bot_username == "token_binding_bot"


@pytest.mark.asyncio
async def test_rejected_token_is_not_stored(
    async_client: AsyncClient, db_session: AsyncSession, bot_project: str
):
    with _patched_telegram(_getme_unauthorized):
        resp = await async_client.post(
            f"/api/projects/{bot_project}/telegram/token",
            json={"token": VALID_TOKEN},
            headers={"X-Telegram-ID": TELEGRAM_ID},
        )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    verdict = resp.json()
    assert verdict["status"] == TokenVerdictStatus.REJECTED.value
    assert verdict["reason_code"] == TokenRejectionReason.INVALID_TOKEN.value
    assert verdict["user_message"]

    assert await _stored_secrets(db_session, bot_project) == {}

    repo = await db_session.scalar(
        select(Repository).where(Repository.project_id == uuid.UUID(bot_project))
    )
    assert repo.bot_username is None


@pytest.mark.asyncio
async def test_token_cannot_be_written_through_the_secrets_endpoint(
    async_client: AsyncClient, db_session: AsyncSession, bot_project: str
):
    resp = await async_client.post(
        f"/api/projects/{bot_project}/config/secrets",
        json={"secrets": {"TELEGRAM_BOT_TOKEN": VALID_TOKEN}},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text

    # Same token under an innocuous key — the shape gives it away.
    disguised = await async_client.post(
        f"/api/projects/{bot_project}/config/secrets",
        json={"secrets": {"SOME_OTHER_KEY": VALID_TOKEN}},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert disguised.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, disguised.text

    assert await _stored_secrets(db_session, bot_project) == {}
