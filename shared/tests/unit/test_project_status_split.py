"""Tests for ProjectStatus/ServiceStatus/RepositoryStatus split.

Step 1: Verify enum values match the domain model.
Step 2: Verify model defaults.
"""

import uuid

from sqlalchemy import create_engine, insert, select
from sqlalchemy.orm import Session

from shared.contracts.dto.project import ProjectStatus, ServiceStatus
from shared.contracts.dto.repository import RepositoryStatus
from shared.models.project import Project
from shared.models.repository import Repository
from shared.models.user import User


class TestProjectStatusEnum:
    """ProjectStatus should only contain lifecycle states."""

    def test_has_exactly_4_values(self):
        assert len(ProjectStatus) == 4

    def test_values(self):
        assert set(ProjectStatus) == {
            ProjectStatus.DRAFT,
            ProjectStatus.ACTIVE,
            ProjectStatus.PAUSED,
            ProjectStatus.ARCHIVED,
        }

    def test_no_process_states(self):
        """Removed states should not exist."""
        removed = {
            "scaffolding",
            "scaffolded",
            "scaffold_failed",
            "developing",
            "testing",
            "deploying",
            "maintenance",
            "error",
            "failed",
            "missing",
        }
        current_values = {s.value for s in ProjectStatus}
        assert current_values.isdisjoint(removed)


class TestServiceStatusEnum:
    """ServiceStatus tracks runtime state of the deployed service."""

    def test_has_exactly_5_values(self):
        assert len(ServiceStatus) == 5

    def test_values(self):
        assert set(ServiceStatus) == {
            ServiceStatus.NOT_DEPLOYED,
            ServiceStatus.RUNNING,
            ServiceStatus.DEGRADED,
            ServiceStatus.DOWN,
            ServiceStatus.STOPPED,
        }


class TestRepositoryStatusEnum:
    """RepositoryStatus tracks whether the repo is accessible."""

    def test_has_exactly_2_values(self):
        assert len(RepositoryStatus) == 2

    def test_values(self):
        assert set(RepositoryStatus) == {
            RepositoryStatus.ACTIVE,
            RepositoryStatus.MISSING,
        }


class TestProjectDTOServiceStatus:
    """ProjectDTO/ProjectCreate/ProjectUpdate should include service_status."""

    def test_project_dto_has_service_status(self):
        from shared.contracts.dto.project import ProjectDTO

        fields = ProjectDTO.model_fields
        assert "service_status" in fields

    def test_project_create_defaults_service_status(self):
        from shared.contracts.dto.project import ProjectCreate

        fields = ProjectCreate.model_fields
        assert "service_status" in fields

    def test_project_update_accepts_service_status(self):
        from shared.contracts.dto.project import ProjectUpdate

        fields = ProjectUpdate.model_fields
        assert "service_status" in fields


def _setup_db():
    """Create in-memory SQLite DB with tables needed for model tests."""
    engine = create_engine("sqlite:///:memory:")
    User.__table__.create(engine)
    Project.__table__.create(engine)
    Repository.__table__.create(engine)
    return engine


class TestProjectModelDefaults:
    """Project model should default service_status to not_deployed."""

    def test_service_status_default(self):
        engine = _setup_db()
        project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        with Session(engine) as session:
            session.execute(insert(User).values(id=1, telegram_id=100, username="tester"))
            session.execute(
                insert(Project).values(
                    id=project_id,
                    name="test",
                    status=ProjectStatus.DRAFT.value,
                    config={},
                    owner_id=1,
                )
            )
            session.commit()

        with Session(engine) as session:
            project = session.execute(select(Project).where(Project.id == project_id)).scalar_one()
            assert project.service_status == ServiceStatus.NOT_DEPLOYED.value


class TestRepositoryModelDefaults:
    """Repository model should default status to active."""

    def test_status_default(self):
        engine = _setup_db()
        project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        with Session(engine) as session:
            session.execute(insert(User).values(id=1, telegram_id=100, username="tester"))
            session.execute(
                insert(Project).values(
                    id=project_id,
                    name="test",
                    status=ProjectStatus.DRAFT.value,
                    config={},
                    owner_id=1,
                )
            )
            session.execute(
                insert(Repository).values(
                    id="repo-1",
                    project_id=project_id,
                    name="test-repo",
                    git_url="https://github.com/test/test",
                )
            )
            session.commit()

        with Session(engine) as session:
            repo = session.execute(select(Repository).where(Repository.id == "repo-1")).scalar_one()
            assert repo.status == RepositoryStatus.ACTIVE.value
