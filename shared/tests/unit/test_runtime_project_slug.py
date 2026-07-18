"""Runtime project slug validation shared by API DTOs and SSH consumers."""

from __future__ import annotations

import uuid

from pydantic import ValidationError
import pytest

from shared.contracts.dto.project import ProjectCreate, ProjectDTO, ProjectStatus, ProjectUpdate
from shared.contracts.runtime_project import RuntimeProjectSlug, runtime_project_slug


def test_runtime_project_slug_accepts_canonical_value() -> None:
    assert runtime_project_slug("my-service-1") == "my-service-1"
    assert RuntimeProjectSlug("my-service-1") == "my-service-1"


@pytest.mark.parametrize(
    "name",
    ["bad; touch /tmp/pwned", "BadName", "bad_name", "bad name", "-bad", "bad/dir"],
)
def test_runtime_project_slug_rejects_non_canonical_values(name: str) -> None:
    with pytest.raises(ValueError, match="invalid runtime project slug"):
        runtime_project_slug(name)


def test_project_create_rejects_malicious_name() -> None:
    with pytest.raises(ValidationError, match="invalid runtime project slug"):
        ProjectCreate(name="bad; touch /tmp/pwned")


def test_project_update_rejects_malicious_name() -> None:
    with pytest.raises(ValidationError, match="invalid runtime project slug"):
        ProjectUpdate(name="bad; touch /tmp/pwned")


def test_project_dto_rejects_malicious_name() -> None:
    with pytest.raises(ValidationError, match="invalid runtime project slug"):
        ProjectDTO(
            id=uuid.uuid4(),
            name="bad; touch /tmp/pwned",
            status=ProjectStatus.ACTIVE,
            owner_id=1,
        )
