"""Shared DTO factory helpers for the supervisor test modules.

Not a test module (no `test_` prefix) — imported by `test_supervisor.py` and
`test_supervisor_run_routing.py` so the factories live in one place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO

_NOW = datetime.now(UTC)


def _make_story(
    *,
    id: str = "story-1",
    project_id: str = "00000000-0000-0000-0000-000000000001",
    title: str = "Test Story",
    status: str = "created",
    priority: int = 0,
    created_by: str = "system",
    type: str = "product",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    **kwargs,
) -> StoryDTO:
    return StoryDTO(
        id=id,
        project_id=UUID(project_id),
        title=title,
        status=status,
        priority=priority,
        created_by=created_by,
        type=type,
        created_at=created_at or _NOW,
        updated_at=updated_at,
        **kwargs,
    )


def _make_task(**overrides) -> TaskDTO:
    defaults = {
        "id": "task-1",
        "project_id": UUID("00000000-0000-0000-0000-000000000001"),
        "type": "feature",
        "title": "Test Task",
        "status": "todo",
        "priority": 0,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "story_id": None,
        "created_at": _NOW,
        "updated_at": None,
    }
    # Allow project_id as string for convenience
    if "project_id" in overrides and isinstance(overrides["project_id"], str):
        overrides["project_id"] = UUID(overrides["project_id"])
    defaults.update(overrides)
    return TaskDTO(**defaults)


def _make_repo(
    *,
    id: str = "repo-1",
    project_id: str = "00000000-0000-0000-0000-000000000001",
    name: str = "test-project",
    git_url: str = "https://github.com/org/test-project",
    role: str = "primary",
    visibility: str = "private",
    is_managed: bool = True,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> RepositoryDTO:
    return RepositoryDTO(
        id=id,
        project_id=UUID(project_id),
        name=name,
        git_url=git_url,
        role=role,
        visibility=visibility,
        is_managed=is_managed,
        created_at=created_at or _NOW,
        updated_at=updated_at,
    )


def _make_run(
    *,
    id: str = "deploy-1",
    project_id: str = "00000000-0000-0000-0000-000000000001",
    type: str = RunType.DEPLOY,
    status: str = RunStatus.COMPLETED,
    story_id: str | None = "story-1",
    result: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> RunDTO:
    return RunDTO(
        id=id,
        project_id=project_id,
        type=type,
        status=status,
        story_id=story_id,
        result=result,
        created_at=created_at or _NOW,
        updated_at=updated_at,
    )


def _invalid_result_error(run_type: str) -> ValidationError:
    """A real ValidationError from a run whose result belongs to another type."""
    other = "qa_outcome" if run_type == "deploy" else "deploy_outcome"
    try:
        RunDTO.model_validate(
            {
                "id": "bad-run",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "type": run_type,
                "status": "failed",
                "result": {other: "passed"},
                "created_at": _NOW.isoformat(),
            }
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


def _terminal_no_result_error(run_type: str) -> ValidationError:
    """A real ValidationError from a terminal run that lost its result."""
    try:
        RunDTO.model_validate(
            {
                "id": "no-result-run",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "type": run_type,
                "status": "completed",
                "result": None,
                "created_at": _NOW.isoformat(),
            }
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")
