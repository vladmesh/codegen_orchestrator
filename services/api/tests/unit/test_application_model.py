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
            "status",
            "last_health_check",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_no_port_column(self):
        """Application should NOT have a port column — ports live in PortAllocation."""
        cols = {c.name for c in Application.__table__.columns}
        assert "port" not in cols

    def test_has_port_allocations_relationship(self):
        """Application should have a port_allocations relationship."""
        from sqlalchemy import inspect

        mapper = inspect(Application)
        assert "port_allocations" in mapper.relationships

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


class TestApplicationHealthFields:
    def test_response_time_ms_column_exists(self):
        cols = {c.name for c in Application.__table__.columns}
        assert "response_time_ms" in cols

    def test_response_time_ms_nullable(self):
        assert Application.__table__.c.response_time_ms.nullable

    def test_ssl_expires_at_column_exists(self):
        cols = {c.name for c in Application.__table__.columns}
        assert "ssl_expires_at" in cols

    def test_ssl_expires_at_nullable(self):
        assert Application.__table__.c.ssl_expires_at.nullable

    def test_uptime_pct_24h_column_exists(self):
        cols = {c.name for c in Application.__table__.columns}
        assert "uptime_pct_24h" in cols

    def test_uptime_pct_24h_nullable(self):
        assert Application.__table__.c.uptime_pct_24h.nullable


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
