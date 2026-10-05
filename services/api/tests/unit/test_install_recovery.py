"""A cancelled old writer is released without changing another cycle's stop."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest
from test_install_contract import install_payload

from shared.contracts.dto.catalog_install import (
    InstallCommand,
    InstallOperation,
    InstallOperatorRequest,
)
from src.catalog_install import install_command
from src.catalog_install_recovery import operator_install_recovery


@pytest.mark.asyncio
async def test_admin_retry_releases_cancelled_old_operation_without_touching_story(monkeypatch):
    now = datetime.now(UTC)
    operation = InstallOperation(
        id="install-old",
        project_id=uuid.uuid4(),
        task_id="task-old",
        story_id="story-1",
        repository_id="repo-1",
        cycle_started_at=now - timedelta(days=1),
        state="recovery_required",
        stage="lease_lost",
        head_sha="a" * 40,
    )
    task = SimpleNamespace(
        id=operation.task_id,
        type="install",
        project_id=operation.project_id,
        repository_id=operation.repository_id,
        status="cancelled",
        install_operation=operation.model_dump(mode="json"),
    )
    cause = {"detail": "A different stop in the current cycle"}
    story = SimpleNamespace(
        id=operation.story_id,
        created_at=operation.cycle_started_at,
        reopened_at=now,
        status="waiting_human_review",
        quarantine_reason=cause,
    )
    monkeypatch.setattr(
        "src.catalog_install_recovery._lock_dispatch_tasks",
        AsyncMock(return_value=(task, {}, None, story.id)),
    )
    monkeypatch.setattr(
        "src.routers._story_helpers._get_story_for_update", AsyncMock(return_value=story)
    )
    monkeypatch.setattr("src.routers.projects_guards.load_locked_project", AsyncMock())
    db = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace()), add=Mock(), commit=AsyncMock()
    )
    answer = await operator_install_recovery(
        task.id,
        InstallOperatorRequest(operation_id=operation.id, action="retry"),
        "admin:1",
        db,
    )
    assert answer.operation.state == "refused" and answer.operation.head_sha == operation.head_sha
    assert task.status == "cancelled" and story.quarantine_reason is cause
    assert story.status == "waiting_human_review" and story.reopened_at == now
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["admit", "refuse"])
async def test_expired_old_cycle_is_settled_without_parking_the_new_cycle(monkeypatch, action):
    now = datetime.now(UTC)
    pid = uuid.uuid4()
    operation = InstallOperation(
        id="install-old",
        project_id=pid,
        task_id="task-old",
        story_id="story-1",
        repository_id="repo-1",
        cycle_started_at=now - timedelta(days=1),
        state="running",
        stage="push",
        head_sha="a" * 40,
        token=uuid.uuid4().hex,
        heartbeat_at=now - timedelta(minutes=20),
    )
    task = SimpleNamespace(
        id=operation.task_id,
        type="install",
        project_id=pid,
        story_id=operation.story_id,
        repository_id=operation.repository_id,
        status="cancelled",
        install=install_payload(),
        install_operation=operation.model_dump(mode="json"),
    )
    story = SimpleNamespace(
        id=operation.story_id,
        created_at=operation.cycle_started_at,
        reopened_at=now,
        status="in_progress",
        quarantine_reason=None,
    )
    repository = SimpleNamespace(
        project_id=pid, is_managed=True, name="notes", git_url="https://github.com/synthetic/notes"
    )
    monkeypatch.setattr(
        "src.catalog_install._lock_dispatch_tasks",
        AsyncMock(return_value=(task, {}, None, story.id)),
    )
    monkeypatch.setattr(
        "src.catalog_install._take_story_roster", AsyncMock(return_value=([], None))
    )
    monkeypatch.setattr(
        "src.routers._story_helpers._get_story_for_update", AsyncMock(return_value=story)
    )
    monkeypatch.setattr("src.routers.projects_guards.load_locked_project", AsyncMock())
    db = SimpleNamespace(scalar=AsyncMock(return_value=repository), commit=AsyncMock())
    answer = await install_command(
        task.id,
        InstallCommand(
            action=action,
            operation_id=operation.id,
            token=operation.token,
            stage="push",
        ),
        db,
    )
    assert answer.operation.state == "recovery_required"
    assert answer.operation.stage == ("lease_lost" if action == "admit" else "push")
    assert task.status == "cancelled" and story.status == "in_progress"
    assert story.reopened_at == now and story.quarantine_reason is None
