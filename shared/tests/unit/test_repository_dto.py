"""Unit tests for Repository DTOs — RepositoryDTO, RepositoryCreate, RepositoryUpdate."""

from datetime import UTC, datetime
from typing import Any
import uuid

from shared.contracts.dto.repository import (
    RepositoryCreate,
    RepositoryDTO,
    RepositoryRole,
    RepositoryUpdate,
    RepositoryVisibility,
)

_NOW = datetime(2026, 3, 17, tzinfo=UTC)
_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestRepositoryDTO:
    """RepositoryDTO should parse API response dicts."""

    SAMPLE_RESPONSE: dict[str, Any] = {
        "id": "repo-abc123",
        "project_id": str(_PROJECT_ID),
        "name": "my-service",
        "git_url": "https://github.com/org/my-service.git",
        "provider_repo_id": 12345,
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    }

    def test_parse_full_response(self):
        dto = RepositoryDTO.model_validate(self.SAMPLE_RESPONSE)
        assert dto.id == "repo-abc123"
        assert dto.project_id == _PROJECT_ID
        assert dto.name == "my-service"
        assert dto.git_url == "https://github.com/org/my-service.git"
        assert dto.provider_repo_id == 12345
        assert dto.role == "primary"
        assert dto.visibility == "private"
        assert dto.is_managed is True

    def test_parse_minimal_response(self):
        minimal = {
            "id": "repo-min",
            "project_id": str(_PROJECT_ID),
            "name": "svc",
            "git_url": "https://github.com/org/svc.git",
            "provider_repo_id": None,
            "role": "dependency",
            "visibility": "public",
            "is_managed": False,
            "created_at": _NOW.isoformat(),
        }
        dto = RepositoryDTO.model_validate(minimal)
        assert dto.provider_repo_id is None
        assert dto.updated_at is None

    def test_model_dump_roundtrip(self):
        dto = RepositoryDTO.model_validate(self.SAMPLE_RESPONSE)
        data = dto.model_dump(mode="json")
        dto2 = RepositoryDTO.model_validate(data)
        assert dto2.id == dto.id


class TestRepositoryCreate:
    def test_minimal(self):
        create = RepositoryCreate(
            project_id=_PROJECT_ID,
            name="my-repo",
            git_url="https://github.com/org/my-repo.git",
        )
        data = create.model_dump(mode="json")
        assert data["role"] == "primary"
        assert data["visibility"] == "private"
        assert data["is_managed"] is True

    def test_full(self):
        create = RepositoryCreate(
            project_id=_PROJECT_ID,
            name="dep-repo",
            git_url="https://github.com/org/dep.git",
            provider_repo_id=999,
            role=RepositoryRole.DEPENDENCY,
            visibility=RepositoryVisibility.PUBLIC,
            is_managed=False,
        )
        data = create.model_dump(mode="json")
        assert data["role"] == "dependency"
        assert data["visibility"] == "public"


class TestRepositoryUpdate:
    def test_exclude_unset(self):
        update = RepositoryUpdate(name="new-name")
        data = update.model_dump(exclude_unset=True)
        assert data == {"name": "new-name"}

    def test_all_fields_optional(self):
        update = RepositoryUpdate()
        data = update.model_dump(exclude_unset=True)
        assert data == {}
