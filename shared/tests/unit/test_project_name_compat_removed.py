"""Project title/slug contract no longer accepts legacy name aliases."""

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest

from shared.contracts.dto.project import ProjectCreate, ProjectDTO, ProjectStatus, ProjectUpdate
from shared.models.project import Project


def test_project_model_has_no_name_synonym():
    assert not hasattr(Project, "name")


def test_project_create_rejects_legacy_name_alias():
    with pytest.raises(ValidationError):
        ProjectCreate.model_validate({"name": "Legacy Name"})


def test_project_update_rejects_legacy_name_alias():
    with pytest.raises(ValidationError):
        ProjectUpdate.model_validate({"name": "Legacy Name"})


def test_project_dto_rejects_legacy_name_alias():
    with pytest.raises(ValidationError):
        ProjectDTO.model_validate(
            {
                "id": uuid.uuid4(),
                "name": "Legacy Name",
                "slug": "legacy-name-0000",
                "status": ProjectStatus.ACTIVE,
                "owner_id": 1,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
