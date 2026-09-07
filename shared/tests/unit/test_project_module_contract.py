"""New scaffold module admission remains narrower than historical project reads."""

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest

from shared.contracts.dto.project import (
    REQUESTABLE_SERVICE_MODULES,
    ProjectCreate,
    ProjectDTO,
    ServiceModule,
)


def test_requestable_modules_match_the_pinned_kit_declaration() -> None:
    assert REQUESTABLE_SERVICE_MODULES == frozenset({ServiceModule.BACKEND, ServiceModule.TG_BOT})


@pytest.mark.parametrize("legacy_module", ["notifications", "frontend"])
def test_new_project_contract_rejects_legacy_modules(legacy_module: str) -> None:
    with pytest.raises(ValidationError, match=f"unsupported scaffold modules: {legacy_module}"):
        ProjectCreate(
            title="New project",
            initiating_run_id="new-project-run",
            config={"modules": ["backend", legacy_module]},
        )


def test_legacy_module_values_remain_readable_from_persisted_projects() -> None:
    now = datetime.now(UTC)
    project = ProjectDTO.model_validate(
        {
            "id": uuid.uuid4(),
            "title": "Historical project",
            "slug": "historical-project",
            "status": "active",
            "modules": ["notifications", "frontend"],
            "owner_id": 1,
            "created_at": now,
            "updated_at": now,
        }
    )

    assert project.modules == [ServiceModule.NOTIFICATIONS, ServiceModule.FRONTEND]
