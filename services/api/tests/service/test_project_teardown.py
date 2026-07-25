"""Teardown is the user's way out of "your own project is holding that token".

The uniqueness check (codegen_orchestrator-711) tells a user their bot is bound to a
project of theirs. Here that verdict is followed through: the holding project is torn
down through the endpoint the PO agent drives, and the same token then binds to the new
project. Teardown also has to refuse a project the caller does not own, and it has to
send the running application on its way down, or the bot keeps polling after the token
has been handed to someone else.
"""

from http import HTTPStatus
import json
import os
from unittest.mock import patch
import uuid

import httpx
from httpx import ASGITransport, AsyncClient
import pytest
from redis.asyncio import Redis

from shared.contracts.dto.application import ApplicationStatus
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.telegram import TokenRejectionReason, TokenVerdictStatus
from shared.contracts.queues.deploy import DeployAction, DeployTrigger
from shared.queues import DEPLOY_QUEUE

OWNER = "100714"
INTRUDER = "100715"
TOKEN = "987654321:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"  # noqa: S105

# Captured before patching: the factory below must not call the patched name.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(scope="module")
async def client():
    """Internal client — used for setup that is not the behaviour under test."""
    from src.dependencies import close_redis, init_redis
    from src.main import app

    await init_redis()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
    ) as c:
        yield c
    await close_redis()


@pytest.fixture
async def user_client(client):
    """A client with no internal key: authorization is decided by X-Telegram-ID alone.

    The PO agent talks to the API exactly like this, so the owner check is only
    proven by a client that cannot claim to be an internal service.
    """
    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def redis():
    from src.config import get_settings

    r = Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield r
    await r.aclose()


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


async def _ensure_user(client: AsyncClient, telegram_id: str) -> None:
    await client.post(
        "/api/users/",
        json={"telegram_id": int(telegram_id), "username": f"user-{telegram_id}"},
    )


async def _make_project(client: AsyncClient, title: str, owner: str = OWNER) -> tuple[str, str]:
    """A project with a primary repository. Returns (project_id, repo_id)."""
    await _ensure_user(client, owner)
    project_id = str(uuid.uuid4())
    resp = await client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": title,
            "status": ProjectStatus.ACTIVE.value,
            "config": {"modules": ["backend", "tg_bot"]},
        },
        headers={"X-Telegram-ID": owner},
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


async def _bind(client: AsyncClient, project_id: str, bot: str, owner: str = OWNER) -> dict:
    with _patched_telegram(bot):
        resp = await client.post(
            f"/api/projects/{project_id}/telegram/token",
            json={"token": TOKEN},
            headers={"X-Telegram-ID": owner},
        )
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()


async def _bound_project(client: AsyncClient, title: str, bot: str) -> tuple[str, str]:
    project_id, repo_id = await _make_project(client, title)
    assert (await _bind(client, project_id, bot))["status"] == TokenVerdictStatus.OK.value
    return project_id, repo_id


async def _server_handle(client: AsyncClient) -> str:
    handle = "test-teardown-server"
    resp = await client.get(f"/api/servers/{handle}")
    if resp.status_code == HTTPStatus.NOT_FOUND:
        created = await client.post(
            "/api/servers/",
            json={
                "handle": handle,
                "host": "teardown.example.com",
                "public_ip": "10.0.0.3",
                "ssh_user": "root",
            },
        )
        assert created.status_code == HTTPStatus.CREATED, created.text
    return handle


async def _add_application(client: AsyncClient, repo_id: str, status_value: str) -> int:
    resp = await client.post(
        "/api/applications/",
        json={
            "repo_id": repo_id,
            "server_handle": await _server_handle(client),
            "service_name": f"svc-{uuid.uuid4().hex[:6]}",
            "status": status_value,
        },
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    return resp.json()["id"]


async def _teardown(client: AsyncClient, project_id: str, actor: str = OWNER) -> httpx.Response:
    return await client.post(
        f"/api/projects/{project_id}/teardown",
        headers={"X-Telegram-ID": actor},
    )


async def _bot_username(client: AsyncClient, repo_id: str) -> str | None:
    resp = await client.get(f"/api/repositories/{repo_id}")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["bot_username"]


async def _secret_keys(client: AsyncClient, project_id: str) -> list[str]:
    resp = await client.get(f"/api/projects/{project_id}/config/secrets/keys")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["keys"]


async def _deploy_messages(redis: Redis, count: int) -> list[dict]:
    entries = await redis.xrevrange(DEPLOY_QUEUE, count=count)
    return [json.loads(fields["data"]) for _entry_id, fields in entries]


@pytest.mark.asyncio
async def test_conflict_with_own_project_resolves_by_freeing_and_reusing_the_token(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """The whole point: the user's own bot comes back to them, and works again."""
    old_id, old_repo_id = await _bound_project(client, "Palindrome", bot)
    new_id, new_repo_id = await _make_project(client, "Echo")

    with _patched_telegram(bot):
        conflict = await client.post(
            f"/api/projects/{new_id}/telegram/token",
            json={"token": TOKEN},
            headers={"X-Telegram-ID": OWNER},
        )
    verdict = conflict.json()
    assert verdict["status"] == TokenVerdictStatus.REJECTED.value
    assert verdict["reason_code"] == TokenRejectionReason.BOUND_TO_OWN_PROJECT.value
    assert verdict["conflict_project_id"] == old_id

    torn = await _teardown(user_client, verdict["conflict_project_id"])
    assert torn.status_code == HTTPStatus.OK, torn.text
    assert torn.json()["released_bot_username"] == bot
    assert torn.json()["status"] == ProjectStatus.ARCHIVED.value

    assert await _bot_username(client, old_repo_id) is None
    assert "TELEGRAM_BOT_TOKEN" not in await _secret_keys(client, old_id)

    reused = await _bind(client, new_id, bot)
    assert reused["status"] == TokenVerdictStatus.OK.value
    assert reused["reason_code"] is None
    assert await _bot_username(client, new_repo_id) == bot
    assert "TELEGRAM_BOT_TOKEN" in await _secret_keys(client, new_id)


@pytest.mark.asyncio
async def test_teardown_sends_the_running_application_down(
    client: AsyncClient, user_client: AsyncClient, redis: Redis, bot: str
):
    """Freeing the token in the database is not enough — the bot must stop polling."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    app_id = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)

    torn = await _teardown(user_client, project_id)
    assert torn.status_code == HTTPStatus.OK, torn.text
    assert torn.json()["undeploying_application_ids"] == [app_id]

    app = await client.get(f"/api/applications/{app_id}")
    assert app.json()["status"] == ApplicationStatus.UNDEPLOYING.value

    published = await _deploy_messages(redis, 1)
    assert published[0]["project_id"] == project_id
    assert published[0]["action"] == DeployAction.UNDEPLOY.value
    assert published[0]["triggered_by"] == DeployTrigger.PO.value


@pytest.mark.asyncio
async def test_teardown_refuses_a_project_owned_by_someone_else(
    client: AsyncClient, user_client: AsyncClient, redis: Redis, bot: str
):
    """Another user's project stays up, keeps its bot, and gets no deploy message."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    app_id = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)
    await _ensure_user(client, INTRUDER)
    before = await _deploy_messages(redis, 1)

    refused = await _teardown(user_client, project_id, actor=INTRUDER)

    assert refused.status_code == HTTPStatus.FORBIDDEN, refused.text
    project = await client.get(f"/api/projects/{project_id}")
    assert project.json()["status"] == ProjectStatus.ACTIVE.value
    assert await _bot_username(client, repo_id) == bot
    app = await client.get(f"/api/applications/{app_id}")
    assert app.json()["status"] == ApplicationStatus.RUNNING.value
    assert await _deploy_messages(redis, 1) == before


@pytest.mark.asyncio
async def test_teardown_without_an_application_still_frees_the_bot(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """A project that never deployed holds the token just as hard."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)

    torn = await _teardown(user_client, project_id)

    assert torn.status_code == HTTPStatus.OK, torn.text
    assert torn.json()["undeploying_application_ids"] == []
    assert torn.json()["released_bot_username"] == bot
    assert await _bot_username(client, repo_id) is None

    new_id, _ = await _make_project(client, "Echo")
    assert (await _bind(client, new_id, bot))["status"] == TokenVerdictStatus.OK.value


@pytest.mark.asyncio
async def test_tearing_down_twice_changes_nothing(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """A user who taps the button twice must not get an error."""
    project_id, _repo_id = await _bound_project(client, "Palindrome", bot)

    await _teardown(user_client, project_id)
    again = await _teardown(user_client, project_id)

    assert again.status_code == HTTPStatus.OK, again.text
    assert again.json()["released_bot_username"] is None
    assert again.json()["undeploying_application_ids"] == []


@pytest.mark.asyncio
async def test_teardown_needs_an_identity(client: AsyncClient, user_client: AsyncClient, bot: str):
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)

    anonymous = await user_client.post(f"/api/projects/{project_id}/teardown")

    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED, anonymous.text
    assert await _bot_username(client, repo_id) == bot
