"""Unit tests for Repository model — column types, defaults, FK."""

from shared.contracts.dto.repository import RepositoryRole
from shared.models.repository import Repository


class TestRepositoryModel:
    def test_tablename(self):
        assert Repository.__tablename__ == "repositories"

    def test_columns_exist(self):
        cols = {c.name for c in Repository.__table__.columns}
        expected = {
            "id",
            "project_id",
            "name",
            "git_url",
            "provider_repo_id",
            "role",
            "is_managed",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols)

    def test_project_id_fk(self):
        col = Repository.__table__.c.project_id
        fk_targets = [fk.target_fullname for fk in col.foreign_keys]
        assert "projects.id" in fk_targets

    def test_project_id_not_nullable(self):
        assert not Repository.__table__.c.project_id.nullable

    def test_provider_repo_id_nullable(self):
        assert Repository.__table__.c.provider_repo_id.nullable

    def test_role_default(self):
        col = Repository.__table__.c.role
        assert col.default.arg == RepositoryRole.PRIMARY.value

    def test_is_managed_default(self):
        col = Repository.__table__.c.is_managed
        assert col.default.arg is True


class TestRepositoryRoleEnum:
    def test_values(self):
        assert RepositoryRole.PRIMARY == "primary"
        assert RepositoryRole.DEPENDENCY == "dependency"

    def test_membership(self):
        assert "primary" in list(RepositoryRole)
        assert "dependency" in list(RepositoryRole)
