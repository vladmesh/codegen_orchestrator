"""Unit tests for Repository schemas — validation, defaults, from_attributes."""

from pydantic import ValidationError
import pytest

from src.schemas.repository import RepositoryCreate, RepositoryRead, RepositoryUpdate


class TestRepositoryCreate:
    def test_minimal(self):
        r = RepositoryCreate(
            project_id="proj-1",
            name="my-repo",
            git_url="https://github.com/org/my-repo",
        )
        assert r.role == "primary"
        assert r.is_managed is True
        assert r.provider_repo_id is None

    def test_all_fields(self):
        r = RepositoryCreate(
            project_id="proj-1",
            name="my-repo",
            git_url="https://github.com/org/my-repo",
            provider_repo_id=12345,
            role="dependency",
            is_managed=False,
        )
        assert r.provider_repo_id == 12345
        assert r.role == "dependency"
        assert r.is_managed is False

    def test_missing_required(self):
        with pytest.raises(ValidationError):
            RepositoryCreate(project_id="proj-1", name="my-repo")


class TestRepositoryRead:
    def test_from_attributes(self):
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        now = datetime.now(UTC)
        mock = MagicMock()
        mock.id = "repo-abc123"
        mock.project_id = "proj-1"
        mock.name = "my-repo"
        mock.git_url = "https://github.com/org/my-repo"
        mock.provider_repo_id = 42
        mock.role = "primary"
        mock.is_managed = True
        mock.created_at = now
        mock.updated_at = now

        r = RepositoryRead.model_validate(mock, from_attributes=True)
        assert r.id == "repo-abc123"
        assert r.provider_repo_id == 42


class TestRepositoryUpdate:
    def test_partial(self):
        r = RepositoryUpdate(name="new-name")
        data = r.model_dump(exclude_unset=True)
        assert data == {"name": "new-name"}

    def test_empty(self):
        r = RepositoryUpdate()
        data = r.model_dump(exclude_unset=True)
        assert data == {}
