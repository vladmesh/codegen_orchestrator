"""Unit tests for Deployment model (renamed from ServiceDeployment) and DeploymentResult enum."""

from shared.contracts.dto.deployment import DeploymentResult
from shared.models.deployment import Deployment


class TestDeploymentModel:
    def test_tablename(self):
        """Table name stays 'service_deployments' for backward compat."""
        assert Deployment.__tablename__ == "service_deployments"

    def test_columns_exist(self):
        cols = {c.name for c in Deployment.__table__.columns}
        expected = {
            "id",
            "application_id",
            "project_id",
            "service_name",
            "server_handle",
            "port",
            "result",
            "deployment_info",
            "deployed_sha",
            "deployed_at",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_application_id_fk(self):
        col = Deployment.__table__.c.application_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "applications.id" in fk_targets

    def test_application_id_nullable(self):
        """Nullable for backward compat with existing data."""
        assert Deployment.__table__.c.application_id.nullable

    def test_result_default(self):
        col = Deployment.__table__.c.result
        assert col.default.arg == DeploymentResult.PENDING.value

    def test_server_handle_fk(self):
        col = Deployment.__table__.c.server_handle
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "servers.handle" in fk_targets


class TestDeploymentResultEnum:
    def test_values(self):
        assert DeploymentResult.PENDING == "pending"
        assert DeploymentResult.SUCCESS == "success"
        assert DeploymentResult.FAILED == "failed"
        assert DeploymentResult.CANCELED == "canceled"

    def test_membership(self):
        values = list(DeploymentResult)
        assert len(values) == 4
