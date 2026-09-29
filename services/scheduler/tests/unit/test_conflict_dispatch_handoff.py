"""Focused review reproduction, controlled rows/transport, no DB or Redis connection."""

from datetime import UTC, datetime
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch
import uuid

import pytest
import structlog

from shared.contracts.dto.engineering_dispatch import (
    EngineeringAttemptStartCommand,
    EngineeringAttemptStartRead,
    EngineeringDispatchRead,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.pr_conflict_repair import (
    PR_CONFLICT_REPAIR_KEY,
    PRConflictRepairAttemptCommand,
    PRConflictRepairEvidence,
    repair_task_id,
)
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.models import Project, Repository, Run, Story, Task, TaskEvent
from src.tasks import task_dispatcher as dispatcher
from src.tasks.supervisor.liveness import supervise_failed_tasks, supervise_stuck_tasks

ROOT = Path(__file__).resolve().parents[4]
for name, path in (
    ("review_api", ROOT / "services/api/src"),
    ("review_api.routers", ROOT / "services/api/src/routers"),
):
    module = ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
database_boundary = ModuleType("review_api.database")


async def unused_session():
    yield None


database_boundary.get_async_session = unused_session
sys.modules[database_boundary.__name__] = database_boundary
settlement = importlib.import_module("review_api.routers._pr_conflict_attempt")
task_actions = importlib.import_module("review_api.routers._task_actions")
admission = importlib.import_module("review_api.engineering_dispatch_admission")


@pytest.mark.asyncio
async def test_fast_failed_conflict_retry_survives_original_dispatch_start():
    pid = uuid.uuid4()
    cycle = datetime.now(UTC)
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
    project = Project(id=pid)
    repository = Repository(id="repo", project_id=pid, role="primary")
    run = Run(
        id="attempt-0",
        task_id=tid,
        project_id=pid,
        story_id="story",
        type="engineering",
        status="queued",
        run_metadata={"iteration": 0},
        created_at=cycle,
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
        TaskEvent(
            task_id=tid,
            event_type="note",
            details={PR_CONFLICT_REPAIR_KEY: evidence.model_dump(mode="json")},
        )
    ]
    db = AsyncMock()
    db.get.return_value = repository
    db.add = lambda event: events.append(event)

    async def scalars(statement):
        entity = statement.column_descriptions[0]["entity"]
        return SimpleNamespace(
            all=lambda: events if entity is TaskEvent else [run] if entity is Run else [tid]
        )

    db.scalars.side_effect = scalars
    command = PRConflictRepairAttemptCommand(
        project_id=pid,
        pr_number=3,
        cycle_started_at=cycle,
        task_id=tid,
        attempt_id=run.id,
        expected_iteration=0,
        disposition="failed",
        detail="Fast early failure",
    )
    outcomes = []

    def task_read():
        return SimpleNamespace(
            id=tid,
            status=TaskStatus(task.status),
            story_id=story.id,
            project_id=pid,
            current_iteration=task.current_iteration,
            max_iterations=3,
            type=TaskType.FIX,
            description="Resolve conflicts",
        )

    def run_read():
        return SimpleNamespace(
            id=run.id,
            status=RunStatus(run.status),
            story_id=story.id,
            created_at=cycle,
            run_metadata=run.run_metadata,
            result=EngineeringRunResult.model_validate(run.result),
        )

    async def settle(*args):
        result = await settlement.settle_pr_conflict_attempt(story.id, command, db=db)
        outcomes.append(result.outcome.value)
        return result

    api = AsyncMock()
    api.admit_engineering_dispatch.return_value = EngineeringDispatchRead(
        outcome="refused", reason="executor_unavailable"
    )
    api.get_tasks_by_status.side_effect = lambda status: (
        [task_read()] if status == TaskStatus(task.status) else []
    )
    api.list_runs.side_effect = lambda **kwargs: [run_read()]

    async def transition(task_id, status, actor):
        await task_actions.transition_task(
            task_id, to_status=status.value, body=task_actions.TaskTransition(actor=actor), db=db
        )

    api.transition_task.side_effect = transition

    async def start(command):
        return await admission.start_engineering_attempt(command, db)

    api.start_engineering_attempt.side_effect = start

    stream = AsyncMock()

    async def consumer_finishes_before_publish_response(*args):
        # A fast consumer failure can finish while the dispatcher is suspended
        # after XADD, before its unfenced status write. This is controlled evidence.
        run.status = "failed"
        run.result = {"engineering_status": "failed"}
        await settle()
        assert task.status == "todo" and task.current_iteration == 1

    stream.publish_message.side_effect = consumer_finishes_before_publish_response
    decision = EngineeringDispatchRead(
        outcome="admitted", run_id=run.id, initiating_run_id="request"
    )
    with (
        patch.object(settlement, "get_task_for_update", AsyncMock(return_value=task)),
        patch.object(settlement, "_get_story_for_update", AsyncMock(return_value=story)),
        patch.object(settlement, "load_locked_project", AsyncMock(return_value=project)),
        patch.object(task_actions, "get_task_for_update", AsyncMock(return_value=task)),
        patch.object(task_actions, "to_read", lambda *args: task_read()),
        patch.object(
            admission,
            "_lock_dispatch_tasks",
            AsyncMock(return_value=(task, {tid: task}, None, story.id)),
        ),
        patch(
            "review_api.routers._story_helpers._get_story_for_update", AsyncMock(return_value=story)
        ),
        patch(
            "review_api.routers.projects_guards.load_locked_project",
            AsyncMock(return_value=project),
        ),
        patch.object(admission, "_lock_engineering_runs", AsyncMock(return_value=[run])),
        patch.object(
            dispatcher,
            "resolve_project_recipient",
            AsyncMock(return_value=SimpleNamespace(telegram_chat_id="123")),
        ),
        patch.object(dispatcher, "_enriched_description", AsyncMock(return_value="Repair")),
        patch("src.tasks.supervisor.liveness.settle_pr_repair_attempt", side_effect=settle),
    ):
        assert not await dispatcher._publish_admitted_dispatch(
            api, stream, task_read(), decision, structlog.get_logger()
        )
        for _ in range(3):
            await dispatcher.dispatch_todo_tasks(api, stream)
            await supervise_stuck_tasks(api, stream)
            await supervise_failed_tasks(api, stream)
    assert outcomes == ["retried"]
    assert task.current_iteration == 1
    assert story.status == "in_progress" and story.owner_notification is None
    assert run.status == "failed" and run.run_metadata == {"iteration": 0}
    assert len([e for e in events if "pr_conflict_repair_attempt" in e.details]) == 1
    assert api.admit_engineering_dispatch.await_count == 3
    api.transition_task.assert_not_awaited()
    api.start_engineering_attempt.assert_awaited_once_with(
        EngineeringAttemptStartCommand(task_id=tid, run_id=run.id)
    )
    # Required liveness invariant: a settled retry remains eligible for admission.
    assert task.status == "todo", (
        f"late original dispatch stranded retry: Task={task.status}, iteration=1; "
        f"only Run=FAILED iteration0; recovery outcomes={outcomes}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["publish_start", "live_recovery"])
@pytest.mark.parametrize("result", ["stale", "unavailable", "lost_response"])
async def test_conflict_start_never_falls_back_to_status_only(path, result):
    api = AsyncMock()
    task = SimpleNamespace(id="pr-conflict-test", current_iteration=0)
    command = EngineeringAttemptStartCommand(task_id=task.id, run_id="attempt")
    answer = EngineeringAttemptStartRead(outcome="reused", task_id=task.id, run_id="attempt")
    if result == "stale":
        api.start_engineering_attempt.return_value = EngineeringAttemptStartRead(
            outcome="stale", task_id=task.id, run_id="attempt"
        )
    else:
        api.start_engineering_attempt.side_effect = (
            [OSError("lost committed response"), answer]
            if result == "lost_response"
            else OSError("unavailable")
        )
    if path == "publish_start":
        outcome = await dispatcher._transition_to_in_dev(
            api, task.id, "attempt", structlog.get_logger()
        )
    else:
        outcome = await dispatcher._execute_repair(
            api,
            task,
            EngineeringDispatchRead(
                outcome="repair",
                repair=EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT,
                run_id="attempt",
            ),
            structlog.get_logger(),
        )
    assert outcome is (result == "lost_response")
    api.transition_task.assert_not_awaited()
    assert all(c.args == (command,) for c in api.start_engineering_attempt.await_args_list)
