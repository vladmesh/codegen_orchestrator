"""A stored product-local INSTALL never selects or replans a released catalog module."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.task import TaskStatus, TaskType
from src.consumers import architect
from src.planning_module_jobs import selected_install_closures
from tests.unit.factories import make_story, make_task, make_task_event

REOPENED = datetime(2026, 1, 2, tzinfo=UTC)


def catalog_install(name: str) -> dict:
    return {
        "package": {
            "name": name,
            "distribution": f"codegen-kit-{name}",
            "version": "0.5.0",
            "tag": f"packages/{name}/v0.5.0",
        },
        "libraries": [],
        "binding": {
            "package": name,
            "resource": f"codegen_kit_{name}:bindings/default.yaml",
            "sha256": "a" * 64,
            "functions": [],
        },
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "catalog_digest": "b" * 64,
        "tooling_commit": "c" * 40,
    }


def local_install(name: str) -> dict:
    return {
        "route": "package_source",
        "project_id": str(make_task().project_id),
        "repository_id": "repo-1",
        "commit": "1" * 40,
        "path": f"packages/{name}",
        "package": {"name": name, "distribution": f"codegen-kit-{name}", "version": "0.5.0"},
        "core_version": "2.2.0",
        "python_version": "3.12.0",
        "tooling_commit": "2" * 40,
    }


def install_task(task_id: str, install: dict, status: str = "todo"):
    return make_task(
        id=task_id,
        type=TaskType.INSTALL,
        status=status,
        story_id="story-1",
        repository_id="repo-1",
        install=install,
    )


def test_only_released_catalog_closures_are_a_plan_selection():
    tasks = [
        install_task("task-catalog", catalog_install("reminders")),
        install_task("task-local", local_install("notes-digest")),
        install_task("task-old", catalog_install("weather"), status=TaskStatus.CANCELLED),
    ]
    assert [item.package.name for item in selected_install_closures(tasks)] == ["reminders"]


@pytest.mark.asyncio
async def test_a_replanned_cycle_reinstalls_only_cancelled_catalog_packages():
    story = make_story(id="story-1", reopened_at=REOPENED)
    tasks = [
        install_task("task-local", local_install("notes-digest"), status=TaskStatus.CANCELLED),
        install_task("task-catalog", catalog_install("reminders"), status=TaskStatus.CANCELLED),
    ]
    note = make_task_event(
        event_type="note",
        details={"catalog_install_settlement": {}, "operator_action": "replan"},
        created_at=REOPENED,
    )
    events = AsyncMock(return_value=[note])
    with patch.object(architect.api_client, "get_task_events", events):
        packages = await architect._replanned_install_packages(story, tasks)
    assert packages == ["reminders"]
    events.assert_awaited_once_with("task-catalog")
