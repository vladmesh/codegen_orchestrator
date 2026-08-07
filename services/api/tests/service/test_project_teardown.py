"""Teardown is the user's way out of "your own project is holding that token".

The uniqueness check (codegen_orchestrator-711) tells a user their bot is bound to a
project of theirs. Here that verdict is followed through: the holding project is torn
down through the endpoint the PO agent drives, and the same token then binds to the new
project. The order matters as much as the outcome: the project keeps its bot until the
undeploy reports the containers down, because a bot that is still polling makes Telegram
refuse whoever binds the token second. Teardown also has to refuse a project the caller
does not own.
"""

from collections.abc import Callable
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
from shared.contracts.dto.project import ProjectStatus, TeardownStatus
from shared.contracts.dto.run import RunStatus
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
    """The PO agent: the internal key underneath, the end user in X-Telegram-ID.

    This used to be a keyless client, on the premise that the agent talks to the
    API that way. It does not — `shared/clients/internal_api.py` puts the key on
    every request it makes — and since the global gate went in, a keyless caller
    never reaches the router at all. The owner check is what these tests are
    about, and it is proven exactly as before: `resolve_actor` judges the request
    by the user it names, key or no key.
    """
    from src.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
    ) as c:
        yield c


@pytest.fixture
async def anonymous_client():
    """Nobody: no key, no user. Refused before any handler runs."""
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


def _patched_telegram(username: str, poller_live: Callable[[], bool] = lambda: False):
    """Telegram accepts the token; `poller_live` says whether a bot is on it right now.

    That callable is the whole point of the reuse tests: Telegram answers 409 to
    getUpdates while another process long-polls the token, which is exactly what the
    old project's tg_bot container does until `docker compose down` has run. A test
    that always answers 200 cannot tell a freed token from one still in use.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": username}})
        if method == "getWebhookInfo":
            return httpx.Response(200, json={"ok": True, "result": {"url": ""}})
        if method == "getUpdates":
            if poller_live():
                return httpx.Response(
                    409,
                    json={
                        "ok": False,
                        "error_code": 409,
                        "description": "Conflict: terminated by other getUpdates request",
                    },
                )
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


async def _server_handle(client: AsyncClient, suffix: str = "") -> str:
    handle = f"test-teardown-server{suffix}"
    resp = await client.get(f"/api/servers/{handle}")
    if resp.status_code == HTTPStatus.NOT_FOUND:
        created = await client.post(
            "/api/servers/",
            json={
                "handle": handle,
                "host": f"teardown{suffix}.example.com",
                "public_ip": "10.0.0.3",
                "ssh_user": "root",
            },
        )
        assert created.status_code == HTTPStatus.CREATED, created.text
    return handle


async def _add_application(
    client: AsyncClient, repo_id: str, status_value: str, server_suffix: str = ""
) -> int:
    """An application on a server. A repo can only have one per server, hence the suffix."""
    resp = await client.post(
        "/api/applications/",
        json={
            "repo_id": repo_id,
            "server_handle": await _server_handle(client, server_suffix),
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


async def _teardown_status(
    client: AsyncClient, project_id: str, actor: str = OWNER
) -> httpx.Response:
    return await client.get(
        f"/api/projects/{project_id}/teardown",
        headers={"X-Telegram-ID": actor},
    )


async def _confirm_undeploy(client: AsyncClient, app_id: int) -> None:
    """What the deploy consumer does once `docker compose down -v` has returned."""
    resp = await client.patch(
        f"/api/applications/{app_id}",
        json={"status": ApplicationStatus.NOT_DEPLOYED.value},
    )
    assert resp.status_code == HTTPStatus.OK, resp.text


async def _fail_undeploy(
    client: AsyncClient, project_id: str, error: str, app_id: int | None = None
) -> None:
    """What the deploy consumer does when the SSH teardown comes back non-zero."""
    runs = await client.get(f"/api/runs/?project_id={project_id}&run_type=deploy")
    assert runs.status_code == HTTPStatus.OK, runs.text
    matching = [
        run
        for run in runs.json()
        if app_id is None or run["run_metadata"].get("application_id") == app_id
    ]
    assert matching, f"no deploy run for application {app_id}"
    run_id = matching[0]["id"]
    patched = await client.patch(
        f"/api/runs/{run_id}",
        json={"status": RunStatus.FAILED.value, "error_message": error},
    )
    assert patched.status_code == HTTPStatus.OK, patched.text


async def _cancel_undeploy(client: AsyncClient, project_id: str, app_id: int) -> None:
    """What the deploy consumer does when another deploy holds the project lock."""
    runs = await client.get(f"/api/runs/?project_id={project_id}&run_type=deploy")
    assert runs.status_code == HTTPStatus.OK, runs.text
    run_id = next(
        run["id"] for run in runs.json() if run["run_metadata"].get("application_id") == app_id
    )
    patched = await client.patch(
        f"/api/runs/{run_id}",
        json={
            "status": RunStatus.CANCELLED.value,
            "error_message": "Skipped: another deploy is already in progress",
        },
    )
    assert patched.status_code == HTTPStatus.OK, patched.text


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
    """The whole point: the user's own bot comes back to them, and works again.

    The old project is deployed and its bot is long-polling the token, so the retry
    is only allowed to succeed after the undeploy has actually reported back. The
    Telegram double here answers 409 until then, the way the real one would.
    """
    old_id, old_repo_id = await _bound_project(client, "Palindrome", bot)
    old_app_id = await _add_application(client, old_repo_id, ApplicationStatus.RUNNING.value)
    new_id, new_repo_id = await _make_project(client, "Echo")

    polling = {"live": True}
    telegram = _patched_telegram(bot, lambda: polling["live"])

    with telegram:
        conflict = await client.post(
            f"/api/projects/{new_id}/telegram/token",
            json={"token": TOKEN},
            headers={"X-Telegram-ID": OWNER},
        )
    verdict = conflict.json()
    assert verdict["status"] == TokenVerdictStatus.REJECTED.value
    assert verdict["reason_code"] == TokenRejectionReason.POLLER_ACTIVE.value

    # Stop the bot the way the old project's own poller would be seen to stop: only
    # once the containers are gone. Until then the conflict is the running bot.
    torn = await _teardown(user_client, old_id)
    assert torn.status_code == HTTPStatus.OK, torn.text
    assert torn.json()["status"] == TeardownStatus.PENDING.value
    assert torn.json()["pending_application_ids"] == [old_app_id]

    # Rebinding now would race the still-running bot, so the token is still held.
    assert await _bot_username(client, old_repo_id) == bot
    with telegram:
        too_early = await client.post(
            f"/api/projects/{new_id}/telegram/token",
            json={"token": TOKEN},
            headers={"X-Telegram-ID": OWNER},
        )
    assert too_early.json()["reason_code"] == TokenRejectionReason.POLLER_ACTIVE.value
    assert await _bot_username(client, new_repo_id) is None

    # docker compose down -v returned: the container is gone, the poller with it.
    await _confirm_undeploy(client, old_app_id)
    polling["live"] = False

    settled = await _teardown_status(user_client, old_id)
    assert settled.status_code == HTTPStatus.OK, settled.text
    assert settled.json()["status"] == TeardownStatus.COMPLETED.value
    assert settled.json()["project_status"] == ProjectStatus.ARCHIVED.value

    assert await _bot_username(client, old_repo_id) is None
    assert "TELEGRAM_BOT_TOKEN" not in await _secret_keys(client, old_id)

    with telegram:
        reused = await client.post(
            f"/api/projects/{new_id}/telegram/token",
            json={"token": TOKEN},
            headers={"X-Telegram-ID": OWNER},
        )
    assert reused.json()["status"] == TokenVerdictStatus.OK.value
    assert reused.json()["reason_code"] is None
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
    assert torn.json()["pending_application_ids"] == [app_id]

    app = await client.get(f"/api/applications/{app_id}")
    assert app.json()["status"] == ApplicationStatus.UNDEPLOYING.value

    published = await _deploy_messages(redis, 1)
    assert published[0]["project_id"] == project_id
    assert published[0]["action"] == DeployAction.UNDEPLOY.value
    assert published[0]["triggered_by"] == DeployTrigger.PO.value


@pytest.mark.asyncio
async def test_every_application_gets_its_own_undeploy(
    client: AsyncClient, user_client: AsyncClient, redis: Redis, bot: str
):
    """A project spread over two servers: each application is named in its own message.

    Without a target in the message the consumer picks an application itself, brings
    the same one down twice and leaves the other running — with the bot still polling
    the token the user was told is free.
    """
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    first = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)
    second = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value, "-b")

    torn = await _teardown(user_client, project_id)

    assert torn.status_code == HTTPStatus.OK, torn.text
    assert torn.json()["status"] == TeardownStatus.PENDING.value
    assert sorted(torn.json()["pending_application_ids"]) == sorted([first, second])

    published = await _deploy_messages(redis, 2)
    assert {m["action"] for m in published} == {DeployAction.UNDEPLOY.value}
    assert sorted(m["application_id"] for m in published) == sorted([first, second])

    for app_id in (first, second):
        app = await client.get(f"/api/applications/{app_id}")
        assert app.json()["status"] == ApplicationStatus.UNDEPLOYING.value

    # One down is not enough: the other container can still be running the bot.
    await _confirm_undeploy(client, first)
    half = await _teardown_status(user_client, project_id)
    assert half.json()["status"] == TeardownStatus.PENDING.value
    assert half.json()["pending_application_ids"] == [second]
    assert await _bot_username(client, repo_id) == bot

    await _confirm_undeploy(client, second)
    done = await _teardown_status(user_client, project_id)
    assert done.json()["status"] == TeardownStatus.COMPLETED.value
    assert await _bot_username(client, repo_id) is None


@pytest.mark.asyncio
async def test_one_application_failing_to_come_down_holds_the_token(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """A half-finished teardown is a failure, not a success with one loose end."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    first = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)
    second = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value, "-b")

    await _teardown(user_client, project_id)
    await _confirm_undeploy(client, first)
    await _fail_undeploy(client, project_id, "SSH command failed (exit 1): no such host", second)

    state = await _teardown_status(user_client, project_id)

    assert state.json()["status"] == TeardownStatus.FAILED.value
    assert "no such host" in state.json()["error"]
    assert state.json()["pending_application_ids"] == [second]
    assert await _bot_username(client, repo_id) == bot
    project = await client.get(f"/api/projects/{project_id}")
    assert project.json()["status"] == ProjectStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_teardown_keeps_the_bot_until_the_application_is_down(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """The binding outlives the request: releasing it early would strand the token."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    app_id = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)

    torn = await _teardown(user_client, project_id)

    assert torn.json()["status"] == TeardownStatus.PENDING.value
    assert torn.json()["released_bot_username"] is None
    assert await _bot_username(client, repo_id) == bot
    project = await client.get(f"/api/projects/{project_id}")
    assert project.json()["status"] == ProjectStatus.ACTIVE.value

    # Polling before anything has changed keeps saying the same thing.
    assert (await _teardown_status(user_client, project_id)).json()["status"] == (
        TeardownStatus.PENDING.value
    )
    assert await _bot_username(client, repo_id) == bot

    await _confirm_undeploy(client, app_id)

    done = await _teardown_status(user_client, project_id)
    assert done.json()["status"] == TeardownStatus.COMPLETED.value
    assert await _bot_username(client, repo_id) is None
    project = await client.get(f"/api/projects/{project_id}")
    assert project.json()["status"] == ProjectStatus.ARCHIVED.value


@pytest.mark.asyncio
async def test_a_failed_undeploy_is_reported_not_waited_on(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """A teardown that cannot finish must say so — the bot is still up either way."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    app_id = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)

    await _teardown(user_client, project_id)
    await _fail_undeploy(client, project_id, "SSH command failed (exit 1): no such directory")

    state = await _teardown_status(user_client, project_id)

    assert state.json()["status"] == TeardownStatus.FAILED.value
    assert "no such directory" in state.json()["error"]
    assert state.json()["pending_application_ids"] == [app_id]
    assert await _bot_username(client, repo_id) == bot


@pytest.mark.asyncio
async def test_a_failed_teardown_can_be_asked_for_again(
    client: AsyncClient, user_client: AsyncClient, redis: Redis, bot: str
):
    """Asking twice after a failure is a retry, not a permanent verdict."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    app_id = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)
    await _teardown(user_client, project_id)
    await _fail_undeploy(client, project_id, "SSH command failed (exit 255): connection refused")

    retried = await _teardown(user_client, project_id)

    assert retried.json()["status"] == TeardownStatus.PENDING.value
    assert retried.json()["pending_application_ids"] == [app_id]
    published = await _deploy_messages(redis, 1)
    assert published[0]["project_id"] == project_id
    assert published[0]["action"] == DeployAction.UNDEPLOY.value

    await _confirm_undeploy(client, app_id)
    assert (await _teardown_status(user_client, project_id)).json()["status"] == (
        TeardownStatus.COMPLETED.value
    )
    assert await _bot_username(client, repo_id) is None


@pytest.mark.asyncio
async def test_an_undeploy_the_consumer_skipped_can_be_asked_for_again(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """A run cancelled on the project's deploy lock must not strand the application.

    Two undeploys for one project reach the consumer back to back, so the second can
    find the lock held and be cancelled. That application would sit in `undeploying`
    with nothing left to bring it down, and the teardown would report pending forever.
    """
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    app_id = await _add_application(client, repo_id, ApplicationStatus.RUNNING.value)
    await _teardown(user_client, project_id)
    await _cancel_undeploy(client, project_id, app_id)

    skipped = await _teardown_status(user_client, project_id)
    assert skipped.json()["status"] == TeardownStatus.FAILED.value

    retried = await _teardown(user_client, project_id)
    assert retried.json()["status"] == TeardownStatus.PENDING.value
    assert retried.json()["pending_application_ids"] == [app_id]

    await _confirm_undeploy(client, app_id)
    assert (await _teardown_status(user_client, project_id)).json()["status"] == (
        TeardownStatus.COMPLETED.value
    )
    assert await _bot_username(client, repo_id) is None


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
    assert torn.json()["status"] == TeardownStatus.COMPLETED.value
    assert torn.json()["pending_application_ids"] == []
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
    assert again.json()["status"] == TeardownStatus.COMPLETED.value
    assert again.json()["released_bot_username"] is None
    assert again.json()["pending_application_ids"] == []


@pytest.mark.asyncio
async def test_teardown_needs_an_identity(
    client: AsyncClient, anonymous_client: AsyncClient, bot: str
):
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)

    anonymous = await anonymous_client.post(f"/api/projects/{project_id}/teardown")

    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED, anonymous.text
    assert await _bot_username(client, repo_id) == bot


@pytest.mark.asyncio
async def test_teardown_status_is_owner_checked_too(
    client: AsyncClient, user_client: AsyncClient, bot: str
):
    """The polling call finishes the teardown, so it needs the same owner check."""
    project_id, repo_id = await _bound_project(client, "Palindrome", bot)
    await _ensure_user(client, INTRUDER)

    refused = await _teardown_status(user_client, project_id, actor=INTRUDER)

    assert refused.status_code == HTTPStatus.FORBIDDEN, refused.text
    assert await _bot_username(client, repo_id) == bot
    project = await client.get(f"/api/projects/{project_id}")
    assert project.json()["status"] == ProjectStatus.ACTIVE.value
