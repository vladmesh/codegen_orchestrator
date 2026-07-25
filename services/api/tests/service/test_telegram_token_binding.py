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


def _telegram_ok(username: str, *, webhook_url: str = ""):
    """A token Telegram accepts, with no webhook and no other poller on it."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": username}})
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": webhook_url}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"Unexpected Telegram method: {method}")

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
    with _patched_telegram(_telegram_ok("token_binding_bot")):
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
async def test_token_with_an_external_webhook_is_not_stored(
    async_client: AsyncClient, db_session: AsyncSession, bot_project: str
):
    """Telegram accepts the token, but someone else's webhook is already on it."""
    handler = _telegram_ok("token_binding_bot", webhook_url="https://elsewhere.example/hook")
    with _patched_telegram(handler):
        resp = await async_client.post(
            f"/api/projects/{bot_project}/telegram/token",
            json={"token": VALID_TOKEN},
            headers={"X-Telegram-ID": TELEGRAM_ID},
        )

    assert resp.status_code == status.HTTP_200_OK, resp.text
    verdict = resp.json()
    assert verdict["status"] == TokenVerdictStatus.REJECTED.value
    assert verdict["reason_code"] == TokenRejectionReason.WEBHOOK_ACTIVE.value

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {"secrets": {"TELEGRAM_BOT_TOKEN": VALID_TOKEN}},
        {"secrets": {"SOMETHING": VALID_TOKEN}},
        {"modules": ["backend"], "env": {"TELEGRAM_BOT_TOKEN": VALID_TOKEN}},
        {"modules": ["backend"], "notes": [VALID_TOKEN]},
    ],
    ids=["secrets-known-key", "secrets-disguised", "nested-key", "nested-value"],
)
async def test_project_creation_cannot_carry_a_bot_token(
    async_client: AsyncClient, db_session: AsyncSession, config: dict
):
    await async_client.post(
        "/api/users/",
        json={"telegram_id": int(TELEGRAM_ID), "username": "token-binding-tester"},
    )
    project_id = str(uuid.uuid4())
    resp = await async_client.post(
        "/api/projects/",
        json={"id": project_id, "title": "Sneaky Bot", "config": config},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert await db_session.get(Project, uuid.UUID(project_id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize(
    "config",
    [
        {"secrets": {"TELEGRAM_BOT_TOKEN": VALID_TOKEN}},
        {"secrets": {"SOMETHING": VALID_TOKEN}},
        {"modules": ["backend"], "env": {"TELEGRAM_BOT_TOKEN": VALID_TOKEN}},
    ],
    ids=["secrets-known-key", "secrets-disguised", "nested-key"],
)
async def test_config_update_cannot_carry_a_bot_token(
    async_client: AsyncClient,
    db_session: AsyncSession,
    bot_project: str,
    method: str,
    config: dict,
):
    resp = await getattr(async_client, method)(
        f"/api/projects/{bot_project}",
        json={"config": config},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )

    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, resp.text
    assert await _stored_secrets(db_session, bot_project) == {}


@pytest.mark.asyncio
async def test_config_update_round_trips_stored_secrets(
    async_client: AsyncClient, db_session: AsyncSession, bot_project: str
):
    """A bound token survives the read-modify-write config updates scaffolder does."""
    with _patched_telegram(_telegram_ok("token_binding_bot")):
        bound = await async_client.post(
            f"/api/projects/{bot_project}/telegram/token",
            json={"token": VALID_TOKEN},
            headers={"X-Telegram-ID": TELEGRAM_ID},
        )
    assert bound.status_code == status.HTTP_200_OK, bound.text

    read_back = await async_client.get(
        f"/api/projects/{bot_project}", headers={"X-Telegram-ID": TELEGRAM_ID}
    )
    config = dict(read_back.json()["config"])
    config["workspace_ready"] = True

    resp = await async_client.patch(
        f"/api/projects/{bot_project}",
        json={"config": config},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["config"]["workspace_ready"] is True

    db_session.expire_all()
    assert (await _stored_secrets(db_session, bot_project))["TELEGRAM_BOT_TOKEN"] == VALID_TOKEN

    # An update that omits secrets keeps them too — only the secret endpoints touch them.
    trimmed = await async_client.patch(
        f"/api/projects/{bot_project}",
        json={"config": {"modules": ["backend"]}},
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert trimmed.status_code == status.HTTP_200_OK, trimmed.text

    db_session.expire_all()
    assert (await _stored_secrets(db_session, bot_project))["TELEGRAM_BOT_TOKEN"] == VALID_TOKEN
