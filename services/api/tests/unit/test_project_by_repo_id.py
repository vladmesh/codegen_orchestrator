"""Unit tests for GET /projects/by-repo-id/{repo_id} endpoint."""

from unittest.mock import AsyncMock, MagicMock

from httpx import ASGITransport, AsyncClient
import pytest

from src.database import get_async_session
from src.main import app


def _mock_project(project_id="proj-1", github_repo_id=42):
    """Create a mock Project ORM object."""
    project = MagicMock()
    project.id = project_id
    project.name = "test-project"
    project.status = "discovered"
    project.config = {}
    project.owner_id = None
    project.github_repo_id = github_repo_id
    project.repository_url = None
    project.created_at = None
    project.updated_at = None
    return project


def _mock_session(execute_result=None):
    """Create a mock DB session with configurable execute result."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = execute_result
    session.execute = AsyncMock(return_value=result_mock)

    async def _session_gen():
        yield session

    return session, _session_gen


class TestGetProjectByRepoId:
    """Test GET /projects/by-repo-id/{repo_id} endpoint."""

    @pytest.mark.asyncio
    async def test_returns_project_when_found(self):
        project = _mock_project(github_repo_id=42)
        _, session_gen = _mock_session(execute_result=project)

        app.dependency_overrides[get_async_session] = session_gen
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/projects/by-repo-id/42")

            assert resp.status_code == 200  # noqa: PLR2004
            data = resp.json()
            assert data["name"] == "test-project"
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_404_when_not_found(self):
        _, session_gen = _mock_session(execute_result=None)

        app.dependency_overrides[get_async_session] = session_gen
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/projects/by-repo-id/999")

            assert resp.status_code == 404  # noqa: PLR2004
        finally:
            app.dependency_overrides.clear()
