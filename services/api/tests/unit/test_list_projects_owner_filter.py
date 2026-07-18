"""Unit tests for GET /api/projects/?owner_id= filter."""

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
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
    p.owner_id = owner_id
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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/projects/",
            params={"owner_id": 1},
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "proj-a"


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
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/projects/",
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == HTTPStatus.OK
    data = resp.json()
    assert len(data) == 2
