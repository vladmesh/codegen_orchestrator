"""Unit tests for repositories router — CRUD endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_repo(**overrides):
    now = datetime.now(UTC)
    defaults = {
        "id": "repo-test1",
        "project_id": PROJECT_UUID,
        "name": "test-repo",
        "git_url": "https://github.com/org/test-repo",
        "provider_repo_id": None,
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": now,
        "updated_at": now,
    }
    defaults.update(overrides)

    repo = MagicMock()
    for k, v in defaults.items():
        setattr(repo, k, v)
    return repo


def _mock_session(scalar_one_or_none=None, scalars_all=None):
    session = AsyncMock()

    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=scalar_one_or_none)
    if scalars_all is not None:
        mock_scalars = MagicMock()
        mock_scalars.all = MagicMock(return_value=scalars_all)
        mock_result.scalars = MagicMock(return_value=mock_scalars)

    session.execute = AsyncMock(return_value=mock_result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.delete = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh

    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_repository():
    session = _mock_session()
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/api/repositories/",
            json={
                "project_id": str(PROJECT_UUID),
                "name": "my-repo",
                "git_url": "https://github.com/org/my-repo",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "my-repo"
    assert data["role"] == "primary"
    assert data["is_managed"] is True
    assert data["id"].startswith("repo-")
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_repositories():
    repos = [_make_repo(id="repo-1"), _make_repo(id="repo-2")]
    session = _mock_session(scalars_all=repos)
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/repositories/")

    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_list_repositories_filter_by_project():
    session = _mock_session(scalars_all=[])
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/repositories/?project_id={PROJECT_UUID}")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_repository():
    repo = _make_repo()
    session = _mock_session(scalar_one_or_none=repo)
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/repositories/repo-test1")

    assert resp.status_code == 200
    assert resp.json()["id"] == "repo-test1"


@pytest.mark.asyncio
async def test_get_repository_not_found():
    session = _mock_session(scalar_one_or_none=None)
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/repositories/repo-missing")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_by_provider_id():
    repo = _make_repo(provider_repo_id=42)
    session = _mock_session(scalar_one_or_none=repo)
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/repositories/by-provider-id/42")

    assert resp.status_code == 200
    assert resp.json()["provider_repo_id"] == 42


@pytest.mark.asyncio
async def test_get_by_provider_id_not_found():
    session = _mock_session(scalar_one_or_none=None)
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/repositories/by-provider-id/999")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_repository():
    repo = _make_repo()
    session = _mock_session(scalar_one_or_none=repo)
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            "/api/repositories/repo-test1",
            json={"name": "updated-name"},
        )

    assert resp.status_code == 200
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_repository():
    repo = _make_repo()
    session = _mock_session(scalar_one_or_none=repo)
    app.dependency_overrides[get_async_session] = lambda: session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete("/api/repositories/repo-test1")

    assert resp.status_code == 200
    session.delete.assert_awaited_once()
    session.commit.assert_awaited_once()
