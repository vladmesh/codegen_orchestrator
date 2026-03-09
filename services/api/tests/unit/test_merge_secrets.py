"""Unit tests for POST /api/projects/{id}/config/secrets atomic merge."""

from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_user(user_id=1, telegram_id=12345, is_admin=False):
    u = MagicMock()
    u.id = user_id
    u.telegram_id = telegram_id
    u.is_admin = is_admin
    return u


def _make_project(project_id=PROJECT_UUID, owner_id=1, config=None):
    p = MagicMock()
    p.id = project_id
    p.owner_id = owner_id
    p.config = dict(config) if config else {}
    p.name = "test"
    p.status = "draft"
    return p


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


def _mock_session(project=None, user=None):
    """Build a mock async session with side_effect for execute calls."""
    session = AsyncMock()
    session.commit = AsyncMock()

    results = []

    # First execute: SELECT FOR UPDATE -> project
    proj_result = MagicMock()
    proj_result.scalar_one_or_none = MagicMock(return_value=project)
    results.append(proj_result)

    # Second execute: _resolve_user -> user (only if telegram_id provided)
    if user is not None:
        user_result = MagicMock()
        user_result.scalar_one_or_none = MagicMock(return_value=user)
        results.append(user_result)

    session.execute = AsyncMock(side_effect=results)
    return session


@pytest.mark.asyncio
@patch("src.routers.projects.encrypt_dict", return_value={"KEY_A": "enc-a", "KEY_B": "enc-b"})
@patch("src.routers.projects.decrypt_dict", return_value={"KEY_A": "val-a"})
async def test_merge_secrets_adds_new_key(mock_decrypt, mock_encrypt):
    """POST merges new secret with existing ones."""
    user = _make_user()
    project = _make_project(config={"secrets": {"KEY_A": "enc-a-old"}})
    session = _mock_session(project=project, user=user)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/projects/{PROJECT_UUID}/config/secrets",
            json={"secrets": {"KEY_B": "val-b"}},
            headers={"X-Telegram-ID": "12345"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    body = resp.json()
    assert "KEY_A" in body["keys"]
    assert "KEY_B" in body["keys"]

    mock_decrypt.assert_called_once_with({"KEY_A": "enc-a-old"})
    mock_encrypt.assert_called_once()
    merged = mock_encrypt.call_args[0][0]
    assert "KEY_A" in merged
    assert "KEY_B" in merged


@pytest.mark.asyncio
@patch("src.routers.projects.encrypt_dict", return_value={"KEY_A": "enc-a"})
@patch("src.routers.projects.decrypt_dict", return_value={})
async def test_merge_secrets_with_env_hints(mock_decrypt, mock_encrypt):
    """POST stores env_hints alongside secrets."""
    user = _make_user()
    project = _make_project(config={})
    session = _mock_session(project=project, user=user)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/projects/{PROJECT_UUID}/config/secrets",
            json={
                "secrets": {"KEY_A": "val-a"},
                "env_hints": {"KEY_A": "Some API key"},
            },
            headers={"X-Telegram-ID": "12345"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert project.config["env_hints"]["KEY_A"] == "Some API key"


@pytest.mark.asyncio
async def test_merge_secrets_project_not_found():
    """POST returns 404 for nonexistent project."""
    session = _mock_session(project=None)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/projects/{uuid.UUID('00000000-0000-0000-0000-000000000099')}/config/secrets",
            json={"secrets": {"KEY_A": "val-a"}},
        )

    assert resp.status_code == 404  # noqa: PLR2004


@pytest.mark.asyncio
async def test_merge_secrets_empty_secrets_rejected():
    """POST with empty secrets dict returns 422."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/projects/{PROJECT_UUID}/config/secrets",
            json={"secrets": {}},
        )

    assert resp.status_code == 422  # noqa: PLR2004


@pytest.mark.asyncio
@patch("src.routers.projects.encrypt_dict", side_effect=lambda d: d)
@patch("src.routers.projects.decrypt_dict", side_effect=lambda d: d)
async def test_merge_secrets_assigns_new_dict_object(mock_decrypt, mock_encrypt):
    """merge_secrets must assign a NEW dict to project.config, not the same object.

    This is critical: if the same dict is reassigned, SQLAlchemy won't detect
    the change and will skip the UPDATE (even with MutableDict, belt-and-suspenders).
    """
    original_config = {"modules": ["backend"], "secrets": {"OLD": "val"}}
    project = _make_project(config=original_config)
    original_config_id = id(project.config)
    user = _make_user()
    session = _mock_session(project=project, user=user)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/projects/{PROJECT_UUID}/config/secrets",
            json={"secrets": {"NEW_KEY": "new-val"}},
            headers={"X-Telegram-ID": "12345"},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    # The config dict assigned must be a NEW object, not the original
    assert id(project.config) != original_config_id, (
        "merge_secrets must create a new dict to trigger SQLAlchemy change detection"
    )
    # Original keys preserved
    assert project.config["modules"] == ["backend"]


@pytest.mark.asyncio
@patch("src.routers.projects.encrypt_dict", return_value={"KEY_A": "enc-new"})
@patch("src.routers.projects.decrypt_dict", return_value={"KEY_A": "old-val"})
async def test_merge_secrets_overwrites_existing_key(mock_decrypt, mock_encrypt):
    """POST with existing key updates its value."""
    project = _make_project(config={"secrets": {"KEY_A": "enc-old"}})
    session = _mock_session(project=project)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/projects/{PROJECT_UUID}/config/secrets",
            json={"secrets": {"KEY_A": "new-val"}},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    merged = mock_encrypt.call_args[0][0]
    assert merged["KEY_A"] == "new-val"
