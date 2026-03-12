"""Unit tests for service_status on project schemas and PATCH endpoint."""

from unittest.mock import AsyncMock, MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
import pytest

from shared.contracts.dto.project import ProjectStatus, ServiceStatus
from src.database import get_async_session
from src.main import app
from src.schemas.project import ProjectBase, ProjectRead, ProjectUpdate

PROJECT_UUID = str(uuid.UUID("00000000-0000-0000-0000-000000000001"))


class TestProjectSchemas:
    """Schemas should include service_status field."""

    def test_project_base_has_service_status_default(self):
        p = ProjectBase(id=uuid.uuid4(), name="test")
        assert p.service_status == ServiceStatus.NOT_DEPLOYED.value

    def test_project_read_has_service_status(self):
        assert "service_status" in ProjectRead.model_fields

    def test_project_update_accepts_service_status(self):
        u = ProjectUpdate(service_status=ServiceStatus.RUNNING.value)
        assert u.service_status == ServiceStatus.RUNNING.value

    def test_project_update_service_status_optional(self):
        u = ProjectUpdate()
        assert u.service_status is None


def _make_project(**overrides):
    p = MagicMock()
    p.id = uuid.UUID(PROJECT_UUID)
    p.name = "test-project"
    p.status = ProjectStatus.ACTIVE.value
    p.service_status = ServiceStatus.NOT_DEPLOYED.value
    p.config = {}
    p.owner_id = 1
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


def _mock_session(project):
    session = AsyncMock()
    session.get = AsyncMock(return_value=project)
    session.commit = AsyncMock()

    async def _refresh(obj):
        pass

    session.refresh = _refresh
    return session


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_patch_updates_service_status_independently():
    """PATCH with only service_status should update it without touching status."""
    project = _make_project()
    session = _mock_session(project)

    async def override():
        yield session

    app.dependency_overrides[get_async_session] = override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.patch(
            f"/api/projects/{PROJECT_UUID}",
            json={"service_status": ServiceStatus.RUNNING.value},
        )

    assert resp.status_code == 200  # noqa: PLR2004
    assert project.service_status == ServiceStatus.RUNNING.value
    # status should remain unchanged
    assert project.status == ProjectStatus.ACTIVE.value
