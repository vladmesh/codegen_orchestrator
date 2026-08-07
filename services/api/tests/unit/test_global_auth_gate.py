"""Nothing the app serves answers anonymously except a named allowlist.

The escalation this closes: anything that can reach the API's port — a worker
container on the shared network, most of all — used to `POST /api/users` with
`is_admin: true` and then act as that administrator by sending its own
`X-Telegram-ID`. Neither half works now, and the first half is checked against
`app.routes` rather than a list written by hand, so a router included tomorrow
without going through the gate fails here instead of in production.
"""

from http import HTTPStatus
import re
from unittest.mock import AsyncMock, MagicMock

from fastapi.routing import APIRoute, iter_route_contexts
from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.dependencies import ANONYMOUS_ROUTES, get_raw_redis
from src.main import app

# A body every write route can be handed. It is nonsense for all of them, which
# is the point: the gate has to answer before anything validates a body, so a 422
# here would mean an unauthenticated caller already reached the schema.
JUNK_BODY: dict = {}

_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _concrete(path: str) -> str:
    """`/api/projects/{project_id}` → `/api/projects/1`, so the URL routes."""
    return _PATH_PARAM.sub("1", path)


def _guarded_routes() -> list[tuple[str, str]]:
    """Every method+path the app serves, minus the anonymous allowlist.

    Read off `app.routes` rather than written down here, which is the whole point:
    a router included tomorrow arrives in this list on its own. `include_router`
    stores a lazy branch, so the tree is flattened through FastAPI's own iterator
    to get the paths as they are actually served, prefixes and all.

    Only `APIRoute`s: `/openapi.json`, `/docs` and `/redoc` are FastAPI's own plain
    Starlette routes, which application-level dependencies cannot reach. They serve
    the schema, not the data, and they sit outside `/api`.
    """
    routes = []
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue
        for method in sorted(context.methods):
            if method in {"HEAD", "OPTIONS"}:
                continue
            if (method, context.path) in ANONYMOUS_ROUTES:
                continue
            routes.append((method, context.path))
    return sorted(routes)


GUARDED_ROUTES = _guarded_routes()


@pytest.fixture(autouse=True)
def _override_session():
    """A session that answers nothing.

    If a request ever gets past the gate it will touch this mock and fail loudly
    instead of quietly returning something that looks like a 200.
    """
    session = AsyncMock()

    async def _execute(*args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = _execute

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override
    yield
    app.dependency_overrides.clear()


def test_the_route_table_is_not_empty():
    """A guard on the guard: an import mistake must not silently skip every case."""
    assert len(GUARDED_ROUTES) > 50


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), GUARDED_ROUTES, ids=lambda v: str(v))
async def test_every_route_outside_the_allowlist_refuses_an_anonymous_caller(method, path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.request(method, _concrete(path), json=JUNK_BODY)

    assert resp.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN), (
        f"{method} {path} answered {resp.status_code} to an anonymous caller: {resp.text}"
    )


@pytest.mark.asyncio
async def test_the_allowlist_is_exactly_the_three_routes_it_is_allowed_to_be():
    """Widening the allowlist is a decision, not a refactor. It shows up here."""
    assert ANONYMOUS_ROUTES == frozenset(
        {("GET", "/"), ("GET", "/health"), ("POST", "/api/lk/auth/token")}
    )


@pytest.mark.asyncio
async def test_root_and_health_still_answer_anonymously():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/")).status_code == HTTPStatus.OK
        assert (await client.get("/health")).status_code == HTTPStatus.OK


@pytest.mark.asyncio
async def test_the_lk_token_exchange_still_reaches_its_handler_anonymously():
    """The dashboard has no JWT when it calls this — that is what it is calling for.

    Both the gate and the handler answer 401, so the status alone proves nothing:
    the verdict has to come from the handler, and it says which token it means.
    """
    redis = AsyncMock()
    redis.getdel.return_value = None
    app.dependency_overrides[get_raw_redis] = lambda: redis
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/lk/auth/token", json={"token": "one-time-token"})
    finally:
        app.dependency_overrides.pop(get_raw_redis)

    assert resp.status_code == HTTPStatus.UNAUTHORIZED
    assert resp.json()["detail"] == "Invalid or expired token", resp.text
    redis.getdel.assert_awaited_once_with("lk_token:one-time-token")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/projects/", "/api/runs/", "/api/users/", "/api/servers/", "/api/debug/queues"]
)
async def test_a_telegram_id_alone_is_not_an_identity(path):
    """The forged-identity case: the header names a user, it never proved one."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(path, headers={"X-Telegram-ID": "424242"})

    assert resp.status_code == HTTPStatus.UNAUTHORIZED, resp.text


@pytest.mark.asyncio
async def test_a_bearer_token_that_is_not_ours_is_not_an_identity():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/projects/", headers={"Authorization": "Bearer not-a-jwt"})

    assert resp.status_code == HTTPStatus.UNAUTHORIZED, resp.text


@pytest.mark.asyncio
async def test_an_anonymous_caller_cannot_make_itself_an_admin():
    """The whole escalation, end to end, from outside the trust boundary."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/users/",
            json={"telegram_id": 424242, "username": "intruder", "is_admin": True},
        )
        upserted = await client.post(
            "/api/users/upsert",
            json={"telegram_id": 424242, "username": "intruder", "is_admin": True},
        )

    assert created.status_code == HTTPStatus.UNAUTHORIZED, created.text
    assert upserted.status_code == HTTPStatus.UNAUTHORIZED, upserted.text


@pytest.mark.asyncio
async def test_a_wrong_internal_key_is_not_a_key():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/users/",
            json={"telegram_id": 424244, "username": "forged", "is_admin": True},
            headers={"X-Internal-Key": "not-the-key"},
        )

    assert resp.status_code == HTTPStatus.UNAUTHORIZED, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/users/", "/api/users/upsert"])
async def test_a_caller_past_the_gate_still_cannot_grant_admin(path):
    """Second lock. Getting through the gate is not the right to hand out admin.

    The gate admits internal services and LK dashboard users; only the first may
    decide `is_admin`. This is what stands between a stolen LK token and an
    administrator, so it is tested with the gate itself stood down.
    """
    from src.dependencies import require_authenticated_caller

    app.dependency_overrides[require_authenticated_caller] = lambda: None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                path, json={"telegram_id": 424245, "username": "lk-user", "is_admin": True}
            )
    finally:
        app.dependency_overrides.pop(require_authenticated_caller)

    assert resp.status_code == HTTPStatus.FORBIDDEN, resp.text
