"""Teardown hands the bot back, so the user can reuse their own token.

The uniqueness check (one live project per bot) is only fair if the hold ends when
the project does. Here a bound project is torn down two ways, archived and with its
application undeployed, and each time the binding, the stored token, and the
verdict for a fresh project are checked against a real database. Release runs
again on an already released project to prove it stays a no-op.
"""

from http import HTTPStatus
from unittest.mock import patch
import uuid

import httpx
from httpx import AsyncClient
import pytest

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.telegram import TokenVerdictStatus

OWNER_ID = "100713"
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


async def _make_project(client: AsyncClient, title: str) -> tuple[str, str]:
    """A project with a primary repository. Returns (project_id, repo_id)."""
    await client.post(
        "/api/users/",
        json={"telegram_id": int(OWNER_ID), "username": f"user-{OWNER_ID}"},
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
        headers={"X-Telegram-ID": OWNER_ID},
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.text

    repo_resp = await client.post(
        "/api/repositories/",
        json={
            "project_id": project_id,
            "name": f"repo-{uuid.uuid4().hex[:6]}",
            "git_url": f"pending://{project_id}",
        },
    )
    assert repo_resp.status_code == HTTPStatus.CREATED, repo_resp.text
    return project_id, repo_resp.json()["id"]


async def _bind(client: AsyncClient, project_id: str, bot: str) -> dict:
    with _patched_telegram(bot):
        resp = await client.post(
            f"/api/projects/{project_id}/telegram/token",
            json={"token": TOKEN},
            headers={"X-Telegram-ID": OWNER_ID},
        )
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


async def _bound_project(client: AsyncClient, title: str, bot: str) -> tuple[str, str]:
    project_id, repo_id = await _make_project(client, title)
    assert (await _bind(client, project_id, bot))["status"] == TokenVerdictStatus.OK.value
    return project_id, repo_id


async def _secret_keys(client: AsyncClient, project_id: str) -> list[str]:
    resp = await client.get(f"/api/projects/{project_id}/config/secrets/keys")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["keys"]


async def _bot_username(client: AsyncClient, repo_id: str) -> str | None:
    resp = await client.get(f"/api/repositories/{repo_id}")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["bot_username"]


async def _archive(client: AsyncClient, project_id: str) -> None:
    resp = await client.patch(
        f"/api/projects/{project_id}",
        json={"status": ProjectStatus.ARCHIVED.value},
        headers={"X-Telegram-ID": OWNER_ID},
    )
    assert resp.status_code == HTTPStatus.OK, resp.text


async def _server_handle(client: AsyncClient) -> str:
    handle = "test-release-server"
    resp = await client.get(f"/api/servers/{handle}")
    if resp.status_code == HTTPStatus.NOT_FOUND:
        created = await client.post(
            "/api/servers/",
            json={
                "handle": handle,
                "host": "release.example.com",
                "public_ip": "10.0.0.2",
                "ssh_user": "root",
            },
        )
        assert created.status_code == HTTPStatus.CREATED, created.text
    return handle


@pytest.mark.asyncio
async def test_archiving_releases_the_binding_and_the_token(async_client: AsyncClient, bot: str):
    project_id, repo_id = await _bound_project(async_client, "Palindrome", bot)
    assert await _bot_username(async_client, repo_id) == bot
    assert "TELEGRAM_BOT_TOKEN" in await _secret_keys(async_client, project_id)

    await _archive(async_client, project_id)

    assert await _bot_username(async_client, repo_id) is None
    keys = await _secret_keys(async_client, project_id)
    assert "TELEGRAM_BOT_TOKEN" not in keys
    assert "TELEGRAM_BOT_USERNAME" not in keys


@pytest.mark.asyncio
async def test_the_freed_token_binds_to_a_new_project(async_client: AsyncClient, bot: str):
    """The point of the release: the user's own bot is theirs again."""
    old_id, _ = await _bound_project(async_client, "Palindrome", bot)
    await _archive(async_client, old_id)

    new_id, new_repo_id = await _make_project(async_client, "Echo")
    verdict = await _bind(async_client, new_id, bot)

    assert verdict["status"] == TokenVerdictStatus.OK.value
    assert verdict["reason_code"] is None
    assert await _bot_username(async_client, new_repo_id) == bot
    assert "TELEGRAM_BOT_TOKEN" in await _secret_keys(async_client, new_id)


@pytest.mark.asyncio
async def test_releasing_again_changes_nothing(async_client: AsyncClient, bot: str):
    """A redelivered archive must not fail on a project that already let go."""
    project_id, repo_id = await _bound_project(async_client, "Palindrome", bot)
    await _archive(async_client, project_id)
    await _archive(async_client, project_id)

    assert await _bot_username(async_client, repo_id) is None
    assert "TELEGRAM_BOT_TOKEN" not in await _secret_keys(async_client, project_id)

    new_id, _ = await _make_project(async_client, "Echo")
    assert (await _bind(async_client, new_id, bot))["status"] == TokenVerdictStatus.OK.value


@pytest.mark.asyncio
async def test_undeploying_the_application_releases_the_binding(
    async_client: AsyncClient, bot: str
):
    """The undeploy consumer reports back by patching the application to not_deployed."""
    project_id, repo_id = await _bound_project(async_client, "Palindrome", bot)
    handle = await _server_handle(async_client)
    app_resp = await async_client.post(
        "/api/applications/",
        json={
            "repo_id": repo_id,
            "server_handle": handle,
            "service_name": f"svc-{uuid.uuid4().hex[:6]}",
            "status": ApplicationStatus.RUNNING.value,
        },
    )
    assert app_resp.status_code == HTTPStatus.CREATED, app_resp.text
    app_id = app_resp.json()["id"]

    patched = await async_client.patch(
        f"/api/applications/{app_id}",
        json={"status": ApplicationStatus.NOT_DEPLOYED.value},
    )
    assert patched.status_code == HTTPStatus.OK, patched.text

    assert await _bot_username(async_client, repo_id) is None
    assert "TELEGRAM_BOT_TOKEN" not in await _secret_keys(async_client, project_id)

    new_id, _ = await _make_project(async_client, "Echo")
    assert (await _bind(async_client, new_id, bot))["status"] == TokenVerdictStatus.OK.value


@pytest.mark.asyncio
async def test_stopping_the_application_keeps_the_binding(async_client: AsyncClient, bot: str):
    """Stop is reversible, so the bot stays with the project a redeploy would revive."""
    project_id, repo_id = await _bound_project(async_client, "Palindrome", bot)
    handle = await _server_handle(async_client)
    app_resp = await async_client.post(
        "/api/applications/",
        json={
            "repo_id": repo_id,
            "server_handle": handle,
            "service_name": f"svc-{uuid.uuid4().hex[:6]}",
            "status": ApplicationStatus.RUNNING.value,
        },
    )
    assert app_resp.status_code == HTTPStatus.CREATED, app_resp.text

    patched = await async_client.patch(
        f"/api/applications/{app_resp.json()['id']}",
        json={"status": ApplicationStatus.STOPPED.value},
    )
    assert patched.status_code == HTTPStatus.OK, patched.text

    assert await _bot_username(async_client, repo_id) == bot
    assert "TELEGRAM_BOT_TOKEN" in await _secret_keys(async_client, project_id)
