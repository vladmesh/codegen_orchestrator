"""An internal key authenticates a service; it does not make it anyone's deputy.

Every service now reaches the API through one transport, and that transport puts
`X-Internal-Key` on every request — including the PO agent's and the bot's, which
carry the end user in `X-Telegram-ID`. If the key alone decided access, a Telegram
user could ask the PO agent for a stranger's project and get it: the agent holds
the key, and the owner check used to return early on it.

So the rule the endpoints under `_check_project_access` follow is: a request that
names a user is judged as that user, key or no key. A service call with no user
named still goes through untouched.
"""

from collections.abc import AsyncGenerator
from http import HTTPStatus
import os
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from shared.contracts.dto.project import ProjectStatus

OWNER = "100931"
INTRUDER = "100932"
ADMIN = "100933"


@pytest.fixture(scope="module")
async def service_client() -> AsyncGenerator[AsyncClient, None]:
    """A service: internal key, no user named. Used for setup and for case 3."""
    from src.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
    ) as client:
        yield client


@pytest.fixture(scope="module")
async def agent_client() -> AsyncGenerator[AsyncClient, None]:
    """The PO agent: internal key on every request, end user in X-Telegram-ID.

    Redis is initialized because the teardown endpoint depends on it, and a
    dependency that raises would answer 500 where this test wants to read a 403.
    """
    from src.dependencies import close_redis, init_redis
    from src.main import app

    await init_redis()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Internal-Key": os.environ["INTERNAL_API_KEY"]},
    ) as client:
        yield client
    await close_redis()


async def _ensure_user(client: AsyncClient, telegram_id: str, *, is_admin: bool = False) -> None:
    await client.post(
        "/api/users/",
        json={
            "telegram_id": int(telegram_id),
            "username": f"user-{telegram_id}",
            "is_admin": is_admin,
        },
    )


@pytest.fixture
async def owned_project(service_client) -> str:
    """A project owned by OWNER, with INTRUDER and ADMIN known to the API."""
    await _ensure_user(service_client, OWNER)
    await _ensure_user(service_client, INTRUDER)
    await _ensure_user(service_client, ADMIN, is_admin=True)

    project_id = str(uuid.uuid4())
    resp = await service_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": f"impersonation-{project_id[:8]}",
            "status": ProjectStatus.ACTIVE.value,
            "config": {},
        },
        headers={"X-Telegram-ID": OWNER},
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    return project_id


@pytest.mark.asyncio
async def test_the_key_does_not_open_a_strangers_project(agent_client, owned_project):
    """A user asking an agent for someone else's project is refused, key or no key."""
    headers = {"X-Telegram-ID": INTRUDER}

    read = await agent_client.get(f"/api/projects/{owned_project}", headers=headers)
    assert read.status_code == HTTPStatus.FORBIDDEN, read.text

    keys = await agent_client.get(
        f"/api/projects/{owned_project}/config/secrets/keys", headers=headers
    )
    assert keys.status_code == HTTPStatus.FORBIDDEN, keys.text

    secret = await agent_client.post(
        f"/api/projects/{owned_project}/config/secrets",
        json={"secrets": {"OPENROUTER_API_KEY": "sk-not-yours"}},
        headers=headers,
    )
    assert secret.status_code == HTTPStatus.FORBIDDEN, secret.text

    teardown = await agent_client.post(f"/api/projects/{owned_project}/teardown", headers=headers)
    assert teardown.status_code == HTTPStatus.FORBIDDEN, teardown.text


@pytest.mark.asyncio
async def test_the_owner_still_reaches_their_own_project_through_the_agent(
    agent_client, owned_project
):
    headers = {"X-Telegram-ID": OWNER}

    read = await agent_client.get(f"/api/projects/{owned_project}", headers=headers)
    assert read.status_code == HTTPStatus.OK, read.text
    assert read.json()["id"] == owned_project

    secret = await agent_client.post(
        f"/api/projects/{owned_project}/config/secrets",
        json={"secrets": {"OPENROUTER_API_KEY": "sk-mine"}},
        headers=headers,
    )
    assert secret.status_code == HTTPStatus.OK, secret.text


@pytest.mark.asyncio
async def test_a_service_call_naming_no_user_is_untouched(service_client, owned_project):
    """Scheduler, scaffolder and the workers name no user; they must keep working."""
    read = await service_client.get(f"/api/projects/{owned_project}")
    assert read.status_code == HTTPStatus.OK, read.text

    patched = await service_client.patch(
        f"/api/projects/{owned_project}",
        json={"status": ProjectStatus.ACTIVE.value},
    )
    assert patched.status_code == HTTPStatus.OK, patched.text


@pytest.mark.asyncio
async def test_an_admin_is_still_an_admin(agent_client, owned_project):
    """The rule scopes a named user to their own rights, and an admin's reach is wider."""
    read = await agent_client.get(
        f"/api/projects/{owned_project}", headers={"X-Telegram-ID": ADMIN}
    )
    assert read.status_code == HTTPStatus.OK, read.text
