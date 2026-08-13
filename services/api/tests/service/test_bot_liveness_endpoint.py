"""The liveness surface QA asks instead of being handed a bot token.

Against a real database, through the real router: a project binds a token the
normal way, and the liveness endpoint then answers with a state and a username.
What must never appear in that answer is the token — that is the whole reason
this endpoint exists rather than an endpoint that lends the credential out.
"""

from unittest.mock import patch
import uuid

from fastapi import status
import httpx
from httpx import AsyncClient
import pytest

from shared.contracts.dto.telegram import BotLivenessState, TokenVerdictStatus

TELEGRAM_ID = "100687"
VALID_TOKEN = "987654322:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105

_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patched_telegram(handler):
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    return patch("src.utils.telegram_token.httpx.AsyncClient", factory)


def _telegram(getme):
    """Telegram answering the binding chain, with getMe scripted per test."""

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return getme(request)
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        raise AssertionError(f"Unexpected Telegram method: {method}")

    return handler


def _getme_ok(username: str):
    def getme(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"username": username}})

    return getme


@pytest.fixture
async def bound_project(async_client: AsyncClient) -> tuple[str, str]:
    """A project whose bot token is bound the only way a token can be bound.

    The username is fresh per test on purpose: a bot already held by another live
    project is refused by the uniqueness layer, so a shared name would leave the
    second test's project with no token at all and every assertion below would be
    about that instead of about liveness. The verdict is asserted, not just the
    status code — a rejected binding also answers 200.

    Returns the project id and the username Telegram will report for it.
    """
    await async_client.post(
        "/api/users/",
        json={"telegram_id": int(TELEGRAM_ID), "username": "liveness-tester"},
    )
    project_id = str(uuid.uuid4())
    bot_username = f"liveness_bot_{uuid.uuid4().hex[:10]}"
    project_resp = await async_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": "Liveness Bot",
            "initiating_run_id": "test-run-1",
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
            "name": "liveness-bot",
            "git_url": f"pending://{project_id}",
        },
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert repo_resp.status_code == status.HTTP_201_CREATED, repo_resp.text

    with _patched_telegram(_telegram(_getme_ok(bot_username))):
        bind = await async_client.post(
            f"/api/projects/{project_id}/telegram/token",
            json={"token": VALID_TOKEN},
            headers={"X-Telegram-ID": TELEGRAM_ID},
        )
    assert bind.status_code == status.HTTP_200_OK, bind.text
    assert bind.json()["status"] == TokenVerdictStatus.OK.value, bind.text
    return project_id, bot_username


@pytest.mark.asyncio
async def test_a_live_bot_is_reported_without_the_token(
    async_client: AsyncClient, bound_project: tuple[str, str]
):
    project_id, bot_username = bound_project

    with _patched_telegram(_telegram(_getme_ok(bot_username))):
        resp = await async_client.get(f"/api/projects/{project_id}/telegram/liveness")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["state"] == BotLivenessState.ALIVE.value
    assert body["bot_username"] == bot_username
    assert VALID_TOKEN not in resp.text


@pytest.mark.asyncio
async def test_a_revoked_token_is_reported_as_not_live(
    async_client: AsyncClient, bound_project: tuple[str, str]
):
    project_id, _ = bound_project

    def revoked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with _patched_telegram(_telegram(revoked)):
        resp = await async_client.get(f"/api/projects/{project_id}/telegram/liveness")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    body = resp.json()
    assert body["state"] == BotLivenessState.NOT_LIVE.value
    assert VALID_TOKEN not in resp.text


@pytest.mark.asyncio
async def test_telegram_not_answering_is_not_reported_as_a_dead_bot(
    async_client: AsyncClient, bound_project: tuple[str, str]
):
    project_id, _ = bound_project

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _patched_telegram(_telegram(unreachable)):
        resp = await async_client.get(f"/api/projects/{project_id}/telegram/liveness")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["state"] == BotLivenessState.TELEGRAM_UNREACHABLE.value


@pytest.mark.asyncio
async def test_a_project_with_no_bound_token_says_so(async_client: AsyncClient):
    await async_client.post(
        "/api/users/",
        json={"telegram_id": int(TELEGRAM_ID), "username": "liveness-tester"},
    )
    project_id = str(uuid.uuid4())
    created = await async_client.post(
        "/api/projects/",
        json={
            "initiating_run_id": "test-run-1",
            "id": project_id,
            "title": "No Bot",
            "status": "draft",
            "config": {},
        },
        headers={"X-Telegram-ID": TELEGRAM_ID},
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text

    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Telegram must not be called for a project with no token")

    with _patched_telegram(_telegram(never)):
        resp = await async_client.get(f"/api/projects/{project_id}/telegram/liveness")

    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["state"] == BotLivenessState.NO_TOKEN.value


@pytest.mark.asyncio
async def test_an_unknown_project_is_a_404(async_client: AsyncClient):
    resp = await async_client.get(f"/api/projects/{uuid.uuid4()}/telegram/liveness")
    assert resp.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_a_caller_without_the_internal_key_is_refused(bound_project: tuple[str, str]):
    """The endpoint spends a stored credential, so it is not an ordinary read."""
    from src.main import app

    project_id, _ = bound_project

    async with AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as anonymous:
        resp = await anonymous.get(f"/api/projects/{project_id}/telegram/liveness")

    assert resp.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    )
