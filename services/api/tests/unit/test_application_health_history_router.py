"""Unit tests for application health history endpoints."""

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
        "response_time_ms": None,
        "ssl_expires_at": None,
        "uptime_pct_24h": None,
        "created_at": now,
        "updated_at": now,
        "port_allocations": [],
    }
    defaults.update(overrides)
    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _make_history_entry(**overrides):
    now = datetime.now(UTC)
    defaults = {
        "id": 1,
        "application_id": 1,
        "recorded_at": now,
        "metrics": {"response_time_ms": 120, "status_code": 200, "healthy": True},
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
        if not getattr(obj, "id", None):
            obj.id = 1
        now = datetime.now(UTC)
        if not getattr(obj, "created_at", None):
            obj.created_at = now
        if not getattr(obj, "updated_at", None):
            obj.updated_at = now
        if not getattr(obj, "recorded_at", None):
            obj.recorded_at = now

    session.refresh = _refresh

    return session


@pytest.fixture()
def _override_db():
    yield
    app.dependency_overrides.pop(get_async_session, None)


class TestGetHealthHistory:
    @pytest.mark.asyncio
    async def test_get_history_returns_entries(self, _override_db):
        application = _make_application()
        entries = [
            _make_history_entry(id=1),
            _make_history_entry(id=2, metrics={"response_time_ms": 200, "healthy": True}),
        ]
        session = _mock_session(get_return=application, scalars_all=entries)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/1/health-history?hours=24")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_get_history_app_not_found(self, _override_db):
        session = _mock_session(get_return=None)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/applications/999/health-history")

        assert resp.status_code == 404


class TestCreateHealthHistory:
    @pytest.mark.asyncio
    async def test_create_snapshot(self, _override_db):
        application = _make_application()
        session = _mock_session(get_return=application)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/applications/1/health-history",
                json={"metrics": {"response_time_ms": 120, "status_code": 200, "healthy": True}},
            )

        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_create_snapshot_app_not_found(self, _override_db):
        session = _mock_session(get_return=None)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/applications/999/health-history",
                json={"metrics": {"response_time_ms": 120}},
            )

        assert resp.status_code == 404


class TestDeleteHealthHistory:
    @pytest.mark.asyncio
    async def test_delete_old_entries(self, _override_db):
        session = _mock_session()
        mock_result = MagicMock()
        mock_result.rowcount = 5
        session.execute = AsyncMock(return_value=mock_result)
        app.dependency_overrides[get_async_session] = lambda: session

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete("/api/applications/health-history?retention_hours=168")

        assert resp.status_code == 200
        assert resp.json()["deleted"] == 5
