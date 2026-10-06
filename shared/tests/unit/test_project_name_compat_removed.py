"""Project title/slug contract no longer accepts legacy name aliases."""

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest

from shared.contracts.dto.project import ProjectCreate, ProjectDTO, ProjectStatus, ProjectUpdate


def test_every_project_contract_rejects_the_legacy_name_alias():
    for model in (ProjectCreate, ProjectUpdate):
        with pytest.raises(ValidationError):
            model.model_validate({"name": "Legacy Name"})
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
