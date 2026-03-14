"""Unit tests for Application model — columns, FKs, defaults, enum."""

from shared.contracts.dto.application import ApplicationStatus
from shared.models.application import Application


class TestApplicationModel:
    def test_tablename(self):
        assert Application.__tablename__ == "applications"

    def test_columns_exist(self):
        cols = {c.name for c in Application.__table__.columns}
        expected = {
            "id",
            "repo_id",
            "server_handle",
            "service_name",
            "port",
            "status",
            "last_health_check",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_repo_id_fk(self):
        col = Application.__table__.c.repo_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "repositories.id" in fk_targets

    def test_server_handle_fk(self):
        col = Application.__table__.c.server_handle
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "servers.handle" in fk_targets

    def test_repo_id_not_nullable(self):
        assert not Application.__table__.c.repo_id.nullable

    def test_server_handle_not_nullable(self):
        assert not Application.__table__.c.server_handle.nullable

    def test_status_default(self):
        col = Application.__table__.c.status
        assert col.default.arg == ApplicationStatus.NOT_DEPLOYED.value

    def test_last_health_check_nullable(self):
        assert Application.__table__.c.last_health_check.nullable

    def test_unique_constraint_repo_server(self):
        """Application should have a unique constraint on (repo_id, server_handle)."""
        unique_constraints = [
            c
            for c in Application.__table__.constraints
            if hasattr(c, "columns") and len(c.columns) > 1
        ]
        constraint_cols = None
        for uc in unique_constraints:
            cols = {c.name for c in uc.columns}
            if cols == {"repo_id", "server_handle"}:
                constraint_cols = cols
                break
        assert constraint_cols == {"repo_id", "server_handle"}


class TestApplicationStatusEnum:
    def test_values(self):
        assert ApplicationStatus.NOT_DEPLOYED == "not_deployed"
        assert ApplicationStatus.RUNNING == "running"
        assert ApplicationStatus.STOPPED == "stopped"
        assert ApplicationStatus.DOWN == "down"
        assert ApplicationStatus.DEGRADED == "degraded"

    def test_membership(self):
        values = list(ApplicationStatus)
        assert len(values) == 5
