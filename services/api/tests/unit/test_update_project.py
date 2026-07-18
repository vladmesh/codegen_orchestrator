"""Unit tests for project update immutability."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app

PROJECT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_project():
    project = MagicMock()
    project.id = PROJECT_UUID
    project.title = "Stable Title"
    project.slug = "stable-title-0000"
    project.status = "draft"
    project.config = {}
    project.owner_id = 1
    project.created_at = datetime.now(UTC)
    project.updated_at = datetime.now(UTC)
    return project


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_patch_project_rejects_slug_update():
    """PATCH rejects slug changes because slugs are immutable."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=_make_project())

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/api/projects/{PROJECT_UUID}",
            json={"slug": "changed-slug"},
            headers={"X-Internal-Key": "test-internal-key"},
        )

    assert resp.status_code == 422  # noqa: PLR2004
    session.commit.assert_not_called()
