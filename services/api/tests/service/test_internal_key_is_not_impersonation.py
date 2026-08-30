"""An internal key authenticates a service; it does not make it anyone's deputy.

Every service now reaches the API through one transport, and that transport puts
`X-Internal-Key` on every request — including the PO agent's and the bot's, which
carry the end user in `X-Telegram-ID`. If the key alone decided access, a Telegram
user could ask the PO agent for a stranger's project and get it: the agent holds
the key, and the owner check used to return early on it.

So the rule the endpoints using `check_project_access` follow is: a request that
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
from src.dependencies import create_lk_jwt

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


@pytest.fixture(scope="module")
async def bearer_client() -> AsyncGenerator[AsyncClient, None]:
    """An LK caller has a bearer, never the internal service credential."""
    from src.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


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
            "initiating_run_id": "test-run-1",
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
async def test_an_internal_key_can_still_name_the_user_who_owns_a_project(
    agent_client, owned_project
):
    """The internal service principal still delegates only when it supplies the key."""
    read = await agent_client.get(
        f"/api/projects/{owned_project}", headers={"X-Telegram-ID": OWNER}
    )

    assert read.status_code == HTTPStatus.OK, read.text


@pytest.mark.asyncio
async def test_an_admin_is_still_an_admin(agent_client, owned_project):
    """The rule scopes a named user to their own rights, and an admin's reach is wider."""
    read = await agent_client.get(
        f"/api/projects/{owned_project}", headers={"X-Telegram-ID": ADMIN}
    )
    assert read.status_code == HTTPStatus.OK, read.text


# ---------------------------------------------------------------------------
# Runs, the same rule from the other side
#
# A run id reaches the PO agent inside a user's message, so `get_run_status` is
# the same shape of request as `get_project`: untrusted id, named user, and a
# transport that adds the key underneath.
# ---------------------------------------------------------------------------


async def _user_id(client: AsyncClient, telegram_id: str) -> int:
    resp = await client.get(f"/api/users/by-telegram/{telegram_id}")
    assert resp.status_code == HTTPStatus.OK, resp.text
    return resp.json()["id"]


@pytest.fixture
async def owned_run(service_client, owned_project) -> str:
    """A run belonging to OWNER."""
    run_id = f"deploy-{uuid.uuid4().hex[:8]}"
    resp = await service_client.post(
        "/api/runs/",
        json={
            "id": run_id,
            "type": "deploy",
            "project_id": owned_project,
            "user_id": await _user_id(service_client, OWNER),
        },
    )
    assert resp.status_code == HTTPStatus.CREATED, resp.text
    return run_id


@pytest.mark.asyncio
async def test_the_key_does_not_open_a_strangers_run(agent_client, owned_run):
    """`get_run_status` with a foreign run id is refused, key or no key."""
    headers = {"X-Telegram-ID": INTRUDER}

    read = await agent_client.get(f"/api/runs/{owned_run}", headers=headers)
    assert read.status_code == HTTPStatus.FORBIDDEN, read.text

    listed = await agent_client.get("/api/runs/", headers=headers)
    assert listed.status_code == HTTPStatus.OK, listed.text
    assert owned_run not in [run["id"] for run in listed.json()]


@pytest.mark.asyncio
async def test_the_owner_still_reads_their_own_run_through_the_agent(agent_client, owned_run):
    read = await agent_client.get(f"/api/runs/{owned_run}", headers={"X-Telegram-ID": OWNER})
    assert read.status_code == HTTPStatus.OK, read.text
    assert read.json()["id"] == owned_run


@pytest.mark.asyncio
async def test_a_service_call_naming_no_user_still_reads_and_writes_runs(service_client, owned_run):
    """The consumers that drive runs name no user; they must keep working."""
    read = await service_client.get(f"/api/runs/{owned_run}")
    assert read.status_code == HTTPStatus.OK, read.text

    patched = await service_client.patch(f"/api/runs/{owned_run}", json={"status": "running"})
    assert patched.status_code == HTTPStatus.OK, patched.text


def _bearer_headers(user_id: int, foreign_telegram_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_lk_jwt(user_id)}",
        "X-Telegram-ID": foreign_telegram_id,
    }


@pytest.mark.asyncio
async def test_a_bearer_cannot_use_a_foreign_telegram_id_to_reach_projects(
    service_client, bearer_client, owned_project
):
    """Both project guards resolve the bearer owner before reading a route's data."""
    intruder_id = await _user_id(service_client, INTRUDER)
    headers = _bearer_headers(intruder_id, OWNER)

    read = await bearer_client.get(f"/api/projects/{owned_project}", headers=headers)
    listed = await bearer_client.get("/api/projects/", headers=headers)

    assert read.status_code == HTTPStatus.FORBIDDEN, read.text
    assert listed.status_code == HTTPStatus.FORBIDDEN, listed.text


@pytest.mark.asyncio
async def test_a_bearer_cannot_create_a_project_for_a_foreign_telegram_user(
    service_client, bearer_client, owned_project
):
    """Project ownership and admission are both bound to the token subject."""
    intruder_id = await _user_id(service_client, INTRUDER)
    created = await bearer_client.post(
        "/api/projects/",
        json={
            "id": str(uuid.uuid4()),
            "title": "not-the-foreign-owner",
            "initiating_run_id": "test-run-foreign-owner",
            "status": ProjectStatus.ACTIVE.value,
            "config": {},
        },
        headers=_bearer_headers(intruder_id, OWNER),
    )

    assert created.status_code == HTTPStatus.FORBIDDEN, created.text


@pytest.mark.asyncio
async def test_a_bearer_creates_a_project_as_its_token_subject_without_a_telegram_header(
    service_client, bearer_client, owned_project
):
    """A bearer replaces the formerly required Telegram header as the owner source."""
    intruder_id = await _user_id(service_client, INTRUDER)
    project_id = str(uuid.uuid4())
    created = await bearer_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": "bearer-token-owner",
            "initiating_run_id": "test-run-bearer-owner",
            "status": ProjectStatus.ACTIVE.value,
            "config": {},
        },
        headers={"Authorization": f"Bearer {create_lk_jwt(intruder_id)}"},
    )

    assert created.status_code == HTTPStatus.CREATED, created.text
    assert created.json()["owner_id"] == intruder_id


@pytest.mark.asyncio
async def test_an_internal_key_can_create_a_project_for_its_named_user(
    agent_client, service_client
):
    """The bot's internal-key path still names the project owner explicitly."""
    await _ensure_user(service_client, OWNER)
    project_id = str(uuid.uuid4())
    created = await agent_client.post(
        "/api/projects/",
        json={
            "id": project_id,
            "title": "internal-key-named-owner",
            "initiating_run_id": "test-run-internal-owner",
            "status": ProjectStatus.ACTIVE.value,
            "config": {},
        },
        headers={"X-Telegram-ID": OWNER},
    )

    assert created.status_code == HTTPStatus.CREATED, created.text
    assert created.json()["owner_id"] == await _user_id(service_client, OWNER)


@pytest.mark.asyncio
async def test_a_non_admin_bearer_cannot_read_allocations_without_a_telegram_header(
    service_client, bearer_client, owned_project
):
    """An omitted client header cannot turn an LK bearer into an allocation admin."""
    intruder_id = await _user_id(service_client, INTRUDER)
    listed = await bearer_client.get(
        "/api/allocations/",
        headers={"Authorization": f"Bearer {create_lk_jwt(intruder_id)}"},
    )

    assert listed.status_code == HTTPStatus.FORBIDDEN, listed.text


@pytest.fixture
async def owned_engineering_attempt(service_client, owned_project) -> str:
    run_id = f"engineering-{uuid.uuid4().hex[:8]}"
    created = await service_client.post(
        "/api/work-admission/paid-runs",
        json={"id": run_id, "type": "engineering", "project_id": owned_project},
    )
    assert created.status_code == HTTPStatus.OK, created.text
    cancelled = await service_client.patch(f"/api/runs/{run_id}", json={"status": "cancelled"})
    assert cancelled.status_code == HTTPStatus.OK, cancelled.text
    return run_id


@pytest.mark.asyncio
async def test_a_bearer_cannot_use_a_foreign_telegram_id_to_widen_run_access(
    service_client, bearer_client, owned_run, owned_engineering_attempt
):
    """Run reads, writes, lists and ledger reads all use the token subject."""
    owner_id = await _user_id(service_client, OWNER)
    intruder_id = await _user_id(service_client, INTRUDER)

    patched = await bearer_client.patch(
        f"/api/runs/{owned_run}",
        json={"status": "running"},
        headers=_bearer_headers(owner_id, ADMIN),
    )
    listed = await bearer_client.get("/api/runs/", headers=_bearer_headers(intruder_id, OWNER))
    ledger = await bearer_client.get(
        "/api/runs/engineering-attempts",
        params={"run_id": owned_engineering_attempt},
        headers=_bearer_headers(intruder_id, OWNER),
    )

    assert patched.status_code == HTTPStatus.FORBIDDEN, patched.text
    assert listed.status_code == HTTPStatus.FORBIDDEN, listed.text
    assert ledger.status_code == HTTPStatus.FORBIDDEN, ledger.text
