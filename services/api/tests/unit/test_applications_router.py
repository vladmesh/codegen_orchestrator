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
        "status": "running",
        "last_health_check": None,
        "created_at": now,
        "updated_at": now,
        "port_allocations": [],
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
        if not getattr(obj, "id", None):
            obj.id = 1
        now = datetime.now(UTC)
        if not getattr(obj, "created_at", None):
            obj.created_at = now
        if not getattr(obj, "updated_at", None):
            obj.updated_at = now
        if not hasattr(obj, "port_allocations") or obj.port_allocations is None:
            obj.port_allocations = []

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
        assert "ports" in data[0]
        assert isinstance(data[0]["ports"], list)


class TestGetApplication:
    @pytest.mark.asyncio
    async def test_get_existing(self, _override_db):
        application = _make_application()
        session = _mock_session(get_return=application)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/1")

        assert resp.status_code == 200
        data = resp.json()
        assert data["service_name"] == "test-bot"
        assert "ports" in data
        assert "port" not in data

    @pytest.mark.asyncio
    async def test_get_not_found(self, _override_db):
        session = _mock_session(get_return=None)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/999")

        assert resp.status_code == 404


class TestCreateApplication:
    @pytest.mark.asyncio
    async def test_create_without_port(self, _override_db):
        """Create application should NOT accept port — ports are managed via PortAllocation."""
        session = _mock_session()
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/applications/",
                json={
                    "repo_id": "repo-test1",
                    "server_handle": "vps-123",
                    "service_name": "test-bot",
                },
            )

        assert resp.status_code == 201


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
