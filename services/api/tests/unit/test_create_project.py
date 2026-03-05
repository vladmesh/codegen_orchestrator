"""Unit tests for POST /api/projects/ ownership enforcement."""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _make_user(user_id=1, telegram_id=12345, is_admin=False):
    u = MagicMock()
    u.id = user_id
    u.telegram_id = telegram_id
    u.is_admin = is_admin
    return u


def _mock_session(existing_project=None, resolve_user="NOT_SET"):
    session = AsyncMock()
    session.get = AsyncMock(return_value=existing_project)
    session.add = MagicMock()
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh

    if resolve_user != "NOT_SET":
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=resolve_user)
        session.execute = AsyncMock(return_value=mock_result)

    return session


PROJECT_PAYLOAD = {
    "id": "test-123",
    "name": "my-project",
    "status": "draft",
    "config": {"modules": ["backend"]},
}


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_project_without_header_sets_no_owner():
    """POST without X-Telegram-ID creates project with owner_id=None."""
    session = _mock_session(existing_project=None)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/projects/", json=PROJECT_PAYLOAD)

    assert resp.status_code == 201  # noqa: PLR2004
    session.add.assert_called_once()
    project = session.add.call_args[0][0]
    assert project.owner_id is None


@pytest.mark.asyncio
async def test_create_project_with_header_sets_owner():
    """POST /api/projects/ with valid X-Telegram-ID sets owner_id."""
    user = _make_user(user_id=5, telegram_id=42000)
    session = _mock_session(existing_project=None, resolve_user=user)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/projects/",
            json=PROJECT_PAYLOAD,
            headers={"X-Telegram-ID": "42000"},
        )

    assert resp.status_code == 201  # noqa: PLR2004
    session.add.assert_called_once()
    project = session.add.call_args[0][0]
    assert project.owner_id == 5  # noqa: PLR2004


@pytest.mark.asyncio
async def test_create_project_unknown_user_returns_404():
    """POST with X-Telegram-ID for non-existent user returns 404."""
    session = _mock_session(existing_project=None, resolve_user=None)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/projects/",
            json=PROJECT_PAYLOAD,
            headers={"X-Telegram-ID": "99999"},
        )

    assert resp.status_code == 404  # noqa: PLR2004
