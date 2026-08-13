"""Unit tests for GET /api/projects/?owner_id= filter."""

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from src.database import get_async_session
from src.main import app


def _make_project(name: str, owner_id: int):
    p = MagicMock()
    p.id = uuid.uuid4()
    p.title = name
    p.slug = f"{name}-0000"
    p.status = "draft"
    p.config = {}
    p.project_spec = None
    p.owner_id = owner_id
    p.initiating_run_id = "test-run-1"
    return p


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_list_projects_with_owner_id_filter():
    """GET /api/projects/?owner_id=1 returns only that owner's projects."""
    p1 = _make_project("proj-a", owner_id=1)
    p2 = _make_project("proj-b", owner_id=2)

    session = AsyncMock()

    captured_queries = []

    async def _execute(query):
        captured_queries.append(query)
        result = MagicMock()
        scalars = MagicMock()
        # Simulate filtering by returning only matching projects
        all_projects = [p1, p2]
        # Check if the query has a WHERE clause with owner_id
        query_str = str(query)
        if "owner_id" in query_str:
            scalars.all.return_value = [p for p in all_projects if p.owner_id == 1]
        else:
            scalars.all.return_value = all_projects
        result.scalars.return_value = scalars
        return result

    session.execute = _execute

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get(
            "/api/projects/",
            params={"owner_id": 1},
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "proj-a"


def _capture_session(captured: list[str], returned: list):
    """Session mock that records each query as SQL with literal values."""
    session = AsyncMock()

    async def _execute(query):
        captured.append(str(query.compile(compile_kwargs={"literal_binds": True})))
        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = returned
        result.scalars.return_value = scalars
        return result

    session.execute = _execute
    return session


def _user(user_id: int, *, is_admin: bool):
    user = MagicMock()
    user.id = user_id
    user.is_admin = is_admin
    return user


@pytest.mark.asyncio
async def test_owner_id_does_not_widen_a_regular_user(monkeypatch):
    """A non-admin passing someone else's owner_id still gets only their own."""
    from src.routers import projects as projects_router

    async def _resolve_actor(*, is_internal, telegram_id, db):
        return _user(7, is_admin=False)

    monkeypatch.setattr(projects_router, "resolve_actor", _resolve_actor)

    captured: list[str] = []
    session = _capture_session(captured, [_make_project("proj-a", owner_id=7)])

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get(
            "/api/projects/",
            params={"owner_id": 2},
            headers={"X-Telegram-ID": "999"},
        )

    assert resp.status_code == HTTPStatus.OK
    assert len(captured) == 1
    assert "owner_id = 7" in captured[0]
    assert "owner_id = 2" not in captured[0]


@pytest.mark.asyncio
async def test_owner_id_still_works_for_an_admin(monkeypatch):
    """The admin panel keeps its cross-owner filter."""
    from src.routers import projects as projects_router

    async def _resolve_actor(*, is_internal, telegram_id, db):
        return _user(1, is_admin=True)

    monkeypatch.setattr(projects_router, "resolve_actor", _resolve_actor)

    captured: list[str] = []
    session = _capture_session(captured, [_make_project("proj-b", owner_id=2)])

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get(
            "/api/projects/",
            params={"owner_id": 2},
            headers={"X-Telegram-ID": "1"},
        )

    assert resp.status_code == HTTPStatus.OK
    assert "owner_id = 2" in captured[0]


@pytest.mark.asyncio
async def test_list_projects_without_owner_id_returns_all():
    """GET /api/projects/ without owner_id returns all projects."""
    p1 = _make_project("proj-a", owner_id=1)
    p2 = _make_project("proj-b", owner_id=2)

    session = AsyncMock()

    async def _execute(query):
        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = [p1, p2]
        result.scalars.return_value = scalars
        return result

    session.execute = _execute

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        resp = await client.get(
            "/api/projects/",
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert len(data) == 2
