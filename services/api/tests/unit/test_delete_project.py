"""Unit tests for DELETE /api/projects/{project_id} endpoint."""

from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_project(project_id=PROJECT_UUID, name="test", owner_id=None):
    """Create a mock Project object."""
    p = MagicMock()
    p.id = project_id
    p.title = name
    p.slug = f"{name}-0000"
    p.status = "draft"
    p.config = {"modules": ["backend"]}
    p.owner_id = owner_id
    return p


def _make_user(user_id=1, telegram_id=12345, is_admin=False):
    """Create a mock User object."""
    u = MagicMock()
    u.id = user_id
    u.telegram_id = telegram_id
    u.is_admin = is_admin
    return u


def _mock_session(project=None, resolve_user=None):
    """Build a mock AsyncSession.

    Args:
        project: What db.get() returns (Project or None).
        resolve_user: What _resolve_user query returns (User or None).
    """
    session = AsyncMock()
    session.get = AsyncMock(return_value=project)
    session.execute = AsyncMock()
    session.delete = AsyncMock()
    session.commit = AsyncMock()

    if resolve_user is not None:
        # _resolve_user does session.execute(select...) then result.scalar_one_or_none()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=resolve_user)
        session.execute = AsyncMock(return_value=mock_result)

    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    """Clean up FastAPI dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_project_not_found():
    """DELETE returns 404 for non-existent project."""
    session = _mock_session(project=None)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            f"/api/projects/{uuid.UUID('00000000-0000-0000-0000-000000000099')}"
        )

    assert resp.status_code == 404  # noqa: PLR2004
    assert resp.json()["detail"] == "Project not found"


@pytest.mark.asyncio
async def test_delete_project_success():
    """DELETE returns 204 and deletes project + related records."""
    project = _make_project()
    session = _mock_session(project=project)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            f"/api/projects/{PROJECT_UUID}",
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == 204  # noqa: PLR2004

    # 3 execute calls: delete runs, delete port_allocations (via app_ids), delete applications
    assert session.execute.call_count == 3  # noqa: PLR2004
    session.delete.assert_called_once_with(project)
    session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_delete_project_access_denied():
    """DELETE returns 403 when non-owner tries to delete."""
    other_user = _make_user(user_id=2, telegram_id=22222, is_admin=False)
    project = _make_project(owner_id=1)  # Owned by user_id=1
    session = _mock_session(project=project, resolve_user=other_user)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            f"/api/projects/{PROJECT_UUID}",
            headers={"X-Telegram-ID": "22222"},
        )

    assert resp.status_code == 403  # noqa: PLR2004


@pytest.mark.asyncio
async def test_delete_project_admin_can_delete():
    """Admin can delete any project regardless of ownership."""
    admin = _make_user(user_id=99, telegram_id=99999, is_admin=True)
    project = _make_project(owner_id=1)  # Owned by someone else
    session = _mock_session(project=project, resolve_user=admin)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(
            f"/api/projects/{PROJECT_UUID}",
            headers={"X-Telegram-ID": "99999"},
        )

    assert resp.status_code == 204  # noqa: PLR2004
    session.delete.assert_called_once_with(project)


@pytest.mark.asyncio
async def test_delete_project_no_auth_header_returns_401():
    """DELETE without any auth header returns 401."""
    project = _make_project()
    session = _mock_session(project=project)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/projects/{PROJECT_UUID}")

    assert resp.status_code == 401  # noqa: PLR2004
