"""A cancelled old writer is released without changing another cycle's stop."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException
import pytest
from test_install_contract import install_payload

from shared.contracts.dto.catalog_install import InstallOperatorRequest
from shared.contracts.dto.install_operation import InstallCommand, InstallOperation
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
        install=install_payload(),
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


def local_task():
    pid = uuid.uuid4()
    return SimpleNamespace(
        id="task-local",
        type="install",
        project_id=pid,
        story_id="story-1",
        repository_id="repo-1",
        status="todo",
        install={
            "route": "package_source",
            "project_id": str(pid),
            "repository_id": "repo-1",
            "commit": "1" * 40,
            "path": "packages/notes_digest",
            "package": {
                "name": "notes-digest",
                "distribution": "codegen-kit-notes-digest",
                "version": "0.1.0",
            },
            "core_version": "2.2.0",
            "python_version": "3.12.0",
            "tooling_commit": "2" * 40,
        },
        install_operation=None,
    )


def stopped(monkeypatch, install, **operation_changes):
    """An INSTALL parked by its operation's refusal: Task and Story waiting for a person."""
    cycle = datetime.now(UTC) - timedelta(hours=1)
    operation = InstallOperation(
        **{
            "id": "install-stopped",
            "project_id": uuid.uuid4(),
            "task_id": "task-stopped",
            "story_id": "story-1",
            "repository_id": "repo-1",
            "cycle_started_at": cycle,
            "state": "refused",
            "stage": "preflight",
            "detail": "preflight_not_read_only: the product changed under preflight",
        }
        | operation_changes
    )
    task = SimpleNamespace(
        id=operation.task_id,
        type="install",
        project_id=operation.project_id,
        story_id=operation.story_id,
        repository_id=operation.repository_id,
        status="waiting_human_review",
        install=install,
        install_operation=operation.model_dump(mode="json"),
    )
    story = SimpleNamespace(
        id=operation.story_id,
        created_at=cycle,
        reopened_at=None,
        status="waiting_human_review",
        engineering_stop=None,
        quarantine_reason={
            "detail": f"Catalog install {operation.id} at preflight: {operation.detail}"
        },
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
    return task, story, db


def local_install():
    return local_task().install


TARGETS = pytest.mark.parametrize(
    "install", [install_payload(), local_install()], ids=["catalog", "local"]
)


@TARGETS
@pytest.mark.asyncio
async def test_retry_releases_a_stopped_install_for_a_new_operation(monkeypatch, install):
    task, story, db = stopped(monkeypatch, install)
    answer = await operator_install_recovery(
        task.id,
        InstallOperatorRequest(operation_id="install-stopped", action="retry"),
        "admin:1",
        db,
    )
    assert answer.outcome == "settled" and answer.operation.id == "install-stopped"
    assert (task.status, task.install_operation, task.install) == ("todo", None, install)
    assert (story.status, story.quarantine_reason) == ("in_progress", None)
    note, *moves = [call.args[0] for call in db.add.call_args_list]
    assert note.details["operator_action"] == "retry"
    assert note.details["catalog_install_settlement"]["id"] == "install-stopped"
    assert [(event.from_status, event.to_status) for event in moves] == [
        ("waiting_human_review", "backlog"),
        ("backlog", "todo"),
    ]
    db.commit.assert_awaited_once()


def other_operation(task, story):
    return InstallOperatorRequest(operation_id="install-other", action="retry")


def other_cycle(task, story):
    story.reopened_at = datetime.now(UTC)


def task_cancelled(task, story):
    task.status = "cancelled"


def task_todo(task, story):
    task.status = "todo"


def story_in_progress(task, story):
    story.status = "in_progress"


def unrelated_cause(task, story):
    story.quarantine_reason = {"detail": "Catalog install install-other at preflight: refused"}


@pytest.mark.parametrize(
    ("violate", "code"),
    [
        (other_operation, "stale_install_recovery"),
        (other_cycle, "stale_install_recovery"),
        (task_cancelled, "stale_install_recovery"),
        (task_todo, "stale_install_recovery"),
        (story_in_progress, "stale_install_recovery"),
        (unrelated_cause, "unrelated_story_stop"),
    ],
    ids=lambda value: getattr(value, "__name__", value),
)
@pytest.mark.asyncio
async def test_a_local_retry_outside_its_stopped_state_changes_nothing(monkeypatch, violate, code):
    task, story, db = stopped(monkeypatch, local_install())
    request = violate(task, story) or InstallOperatorRequest(
        operation_id="install-stopped", action="retry"
    )
    before = (vars(task).copy(), vars(story).copy())
    with pytest.raises(HTTPException) as refused:
        await operator_install_recovery(task.id, request, "admin:1", db)
    assert (refused.value.status_code, refused.value.detail) == (409, {"code": code})
    assert (vars(task), vars(story)) == before
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("state", ["queued", "running", "published"])
@pytest.mark.asyncio
async def test_a_local_retry_of_an_unsettled_or_published_operation_changes_nothing(
    monkeypatch, state
):
    task, story, db = stopped(monkeypatch, local_install(), state=state)
    before = (vars(task).copy(), vars(story).copy())
    with pytest.raises(HTTPException) as refused:
        await operator_install_recovery(
            task.id,
            InstallOperatorRequest(operation_id="install-stopped", action="retry"),
            "admin:1",
            db,
        )
    assert (refused.value.status_code, refused.value.detail) == (
        409,
        {"code": "stale_install_recovery"},
    )
    assert (vars(task), vars(story)) == before
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("action", ["recover", "replan"])
@pytest.mark.asyncio
async def test_a_local_install_is_never_recovered_or_replanned(monkeypatch, action):
    task, story, db = stopped(monkeypatch, local_install())
    before = (vars(task).copy(), vars(story).copy())
    with pytest.raises(HTTPException) as refused:
        await operator_install_recovery(
            task.id,
            InstallOperatorRequest(operation_id="install-stopped", action=action),
            "admin:1",
            db,
            redis=AsyncMock(),
        )
    assert (refused.value.status_code, refused.value.detail) == (
        409,
        {"code": "local_install_unsupported"},
    )
    assert (vars(task), vars(story)) == before
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
