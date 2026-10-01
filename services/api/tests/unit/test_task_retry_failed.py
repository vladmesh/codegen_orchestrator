"""Atomic automatic retry for failed Tasks."""

from datetime import UTC, datetime
import uuid

from fastapi import HTTPException
import pytest

from shared.contracts.dto.task import TaskStatus
from shared.models import Task, TaskEvent
from src.routers import _task_actions
from src.schemas.task import TaskTransition

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = False

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _row: object) -> None:
        return None


def _task(*, current_iteration: int = 0, max_iterations: int = 3) -> Task:
    now = datetime.now(UTC)
    return Task(
        id="task-retry",
        project_id=PROJECT_ID,
        type="feature",
        title="Retry me",
        description=None,
        plan=None,
        status=TaskStatus.FAILED.value,
        priority=0,
        acceptance_criteria=None,
        current_iteration=current_iteration,
        max_iterations=max_iterations,
        need_e2e=False,
        created_by="architect",
        story_id="story-1",
        failure_metadata={"reason": "worker failed"},
        dispatch_admitted=True,
        planning_attempt_id=None,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_failed_retry_commits_status_hops_and_iteration_together(monkeypatch):
    task = _task()
    session = _Session()

    async def locked(task_id, db):
        assert task_id == task.id
        assert db is session
        return task

    monkeypatch.setattr(_task_actions, "get_task_for_update", locked)

    result = await _task_actions.retry_failed_task(
        task.id,
        TaskTransition(actor="supervisor"),
        db=session,
        _=None,
    )

    assert session.committed
    assert (task.status, task.current_iteration, result.status, result.current_iteration) == (
        TaskStatus.TODO.value,
        1,
        TaskStatus.TODO.value,
        1,
    )
    events = [row for row in session.added if isinstance(row, TaskEvent)]
    assert [(event.from_status, event.to_status) for event in events] == [
        (TaskStatus.FAILED, TaskStatus.BACKLOG),
        (TaskStatus.BACKLOG, TaskStatus.TODO),
    ]
    assert events[0].details["previous_iteration"] == 0
    assert events[0].details["iteration"] == 1


@pytest.mark.asyncio
async def test_failed_retry_refuses_exhausted_task(monkeypatch):
    task = _task(current_iteration=3, max_iterations=3)
    session = _Session()

    async def locked(_task_id, _db):
        return task

    monkeypatch.setattr(_task_actions, "get_task_for_update", locked)

    with pytest.raises(HTTPException) as error:
        await _task_actions.retry_failed_task(
            task.id,
            TaskTransition(actor="supervisor"),
            db=session,
            _=None,
        )

    assert error.value.status_code == 409
    assert error.value.detail["reason"] == "task_retry_exhausted"
    assert not session.committed
