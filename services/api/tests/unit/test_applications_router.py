"""Unit tests for applications router — CRUD endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _make_application(**overrides):
    now = datetime.now(UTC)
    defaults = {
        "id": 1,
        "repo_id": "repo-test1",
        "server_handle": "vps-123",
        "service_name": "test-bot",
        "port": 8000,
        "status": "running",
        "last_health_check": None,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _mock_session(get_return=None, scalars_all=None):
    session = AsyncMock()

    mock_result = MagicMock()
    if scalars_all is not None:
        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=scalars_all)
        mock_result.scalars = MagicMock(return_value=mock_scalars)

    session.execute = AsyncMock(return_value=mock_result)
    session.get = AsyncMock(return_value=get_return)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh

    return session


@pytest.fixture()
def _override_db():
    """Fixture that overrides DB session — tests set it via app.dependency_overrides."""
    yield
    app.dependency_overrides.pop(get_async_session, None)


class TestListApplications:
    @pytest.mark.asyncio
    async def test_list_empty(self, _override_db):
        session = _mock_session(scalars_all=[])
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/")

        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_returns_applications(self, _override_db):
        apps = [_make_application(id=1), _make_application(id=2, service_name="bot-2")]
        session = _mock_session(scalars_all=apps)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["service_name"] == "test-bot"


class TestGetApplication:
    @pytest.mark.asyncio
    async def test_get_existing(self, _override_db):
        application = _make_application()
        session = _mock_session(get_return=application)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/1")

        assert resp.status_code == 200
        assert resp.json()["service_name"] == "test-bot"

    @pytest.mark.asyncio
    async def test_get_not_found(self, _override_db):
        session = _mock_session(get_return=None)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/999")

        assert resp.status_code == 404


class TestUpdateApplication:
    @pytest.mark.asyncio
    async def test_update_status(self, _override_db):
        application = _make_application()
        session = _mock_session(get_return=application)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch("/api/applications/1", json={"status": "stopped"})

        assert resp.status_code == 200
        assert application.status == "stopped"
