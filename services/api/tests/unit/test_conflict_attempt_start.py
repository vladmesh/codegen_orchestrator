"""Immutable admission and terminal evidence fence delayed start writes."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest

from shared.contracts.dto.engineering_dispatch import EngineeringAttemptStartCommand
from shared.contracts.dto.pr_conflict_repair import (
    PR_CONFLICT_REPAIR_KEY,
    PRConflictRepairEvidence,
    repair_task_id,
)
from shared.models import Project, Repository, Run, Story, Task, TaskEvent
from src import engineering_dispatch_admission as admission
from src.routers import _story_helpers, _task_actions, projects_guards


@pytest.fixture
def admitted(monkeypatch):
    cycle = datetime.now(UTC)
    pid = uuid.uuid4()
    tid = repair_task_id("story", cycle)
    task = Task(
        id=tid,
        project_id=pid,
        story_id="story",
        status="todo",
        type="fix",
        repository_id="repo",
        dispatch_admitted=True,
        created_by=PR_CONFLICT_REPAIR_KEY,
        current_iteration=0,
        max_iterations=3,
    )
    story = Story(id="story", project_id=pid, status="in_progress", pr_number=3, created_at=cycle)
    run = Run(
        id="attempt",
        task_id=tid,
        story_id="story",
        project_id=pid,
        type="engineering",
        status="queued",
        run_metadata={"iteration": 0},
    )
    evidence = PRConflictRepairEvidence(
        project_id=pid,
        story_id="story",
        pr_number=3,
        cycle_started_at=cycle,
        repository_id="repo",
        head_sha="a" * 40,
        default_branch="main",
        default_sha="b" * 40,
        max_iterations=3,
    )
    events = [
        TaskEvent(task_id=tid, details={PR_CONFLICT_REPAIR_KEY: evidence.model_dump(mode="json")})
    ]
    runs = [run]
    db = AsyncMock()
    db.get.return_value = Repository(id="repo", project_id=pid, role="primary")
    db.add = events.append

    async def scalars(statement):
        entity = statement.column_descriptions[0]["entity"]
        return SimpleNamespace(all=lambda: events if entity is TaskEvent else [tid])

    db.scalars.side_effect = scalars
    monkeypatch.setattr(
        admission,
        "_lock_dispatch_tasks",
        AsyncMock(return_value=(task, {tid: task}, None, story.id)),
    )
    monkeypatch.setattr(_story_helpers, "_get_story_for_update", AsyncMock(return_value=story))
    monkeypatch.setattr(
        projects_guards, "load_locked_project", AsyncMock(return_value=Project(id=pid))
    )
    monkeypatch.setattr(admission, "_lock_engineering_runs", AsyncMock(return_value=runs))
    return task, story, run, events, runs, db


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,expected",
    [
        ("live", "started"),
        ("failed", "terminal_pending"),
        ("settled", "settled"),
        ("new_run", "stale"),
        ("cycle", "stale"),
        ("pr", "stale"),
        ("completed", "completed"),
        ("aborted_prior", "started"),
        ("infrastructure", "priority_pending"),
        ("resource", "priority_pending"),
    ],
)
async def test_start_respects_locked_admission_and_current_attempt(admitted, state, expected):
    task, story, run, events, runs, db = admitted
    if state in {"failed", "settled"}:
        run.status = "failed"
        run.result = {"engineering_status": "failed"}
    if state == "settled":
        task.current_iteration = 1
        events.append(
            TaskEvent(
                task_id=task.id, details={"pr_conflict_repair_attempt": {"attempt_id": run.id}}
            )
        )
    elif state == "new_run":
        runs.append(Run(id="new", task_id=task.id, status="running", run_metadata={"iteration": 1}))
    elif state == "cycle":
        story.reopened_at = story.created_at + timedelta(seconds=1)
    elif state == "pr":
        story.pr_number = 4
    elif state == "completed":
        run.status = "completed"
        run.result = {"engineering_status": "done", "commit_sha": "a" * 40}
    elif state == "aborted_prior":
        runs.append(
            Run(
                id="aborted",
                task_id=task.id,
                status="cancelled",
                run_metadata={"iteration": 0, "pre_handoff_aborted": True},
            )
        )
    elif state in {"infrastructure", "resource"}:
        run.status = "failed"
        run.result = {
            "engineering_status": "failed",
            **(
                {
                    "execution": {
                        "execution_phase": "pre_agent_refused",
                        "infrastructure_refusal": "project_locked",
                    }
                }
                if state == "infrastructure"
                else {"allocation_failure_reason": "impossible_capacity"}
            ),
        }
    before = task.status, task.current_iteration, story.status, len(events)
    command = EngineeringAttemptStartCommand(task_id=task.id, run_id=run.id)
    result = await admission.start_engineering_attempt(command, db)
    assert result.outcome.value == expected
    if expected == "priority_pending":
        assert task.status == "in_dev" and task.current_iteration == 0
        count = len(events)
        assert (await admission.start_engineering_attempt(command, db)).outcome.value == expected
        assert len(events) == count
    elif expected not in {"started", "completed"}:
        assert (task.status, task.current_iteration, story.status, len(events)) == before
    else:
        assert task.status == ("done" if state == "completed" else "in_dev")
        again = await admission.start_engineering_attempt(command, db)
        assert again.outcome.value == ("stale" if state == "completed" else "reused")
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["start", "transition"])
async def test_status_only_conflict_start_is_refused(admitted, monkeypatch, route):
    task, _, _, _, _, db = admitted
    monkeypatch.setattr(_task_actions, "get_task_for_update", AsyncMock(return_value=task))
    with pytest.raises(HTTPException) as error:
        if route == "start":
            await _task_actions.start_task(task.id, db=db)
        else:
            await _task_actions.transition_task(task.id, to_status="in_dev", db=db)
    assert error.value.status_code == 409
    assert task.status == "todo"
    db.commit.assert_not_awaited()
