"""Locked admission owns no-Run refusals; scheduler state hints grant nothing."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

from fastapi import HTTPException
import pytest
from test_conflict_attempt_start import admitted as _admitted

from shared.contracts.dto.engineering_dispatch import EngineeringDispatchCommand
from shared.contracts.dto.work_admission import PaidRunStartRead, WorkAdmissionRead
from shared.models import TaskEvent, WorkAdmissionAudit
from src import engineering_dispatch_admission as admission
from src.work_admission import _audit

admitted = _admitted


def refuse_emergency_stop():
    async def paid(command, session):
        result = await _audit(
            session,
            "paid_work",
            WorkAdmissionRead(outcome="denied", reason="emergency_stop"),
            reference_id=command.id,
            command_payload=command.model_dump(mode="json"),
        )
        return PaidRunStartRead(admission=result)

    admission.start_paid_run.side_effect = paid


@pytest.fixture
def refusal(admitted, monkeypatch):
    task, story, run, events, runs, db = admitted
    runs.clear()
    project = admission.ProjectStatus.ACTIVE
    # The existing fixture owns the locked project reader.
    from src.routers import projects_guards

    projects_guards.load_locked_project.return_value.status = project.value
    projects_guards.load_locked_project.return_value.initiating_run_id = "po-request"
    projects_guards.load_locked_project.return_value.config = {"workspace_ready": True}
    writes = []
    db.add = lambda row: events.append(row) if isinstance(row, TaskEvent) else writes.append(row)
    db.execute.return_value = SimpleNamespace(all=lambda: [])

    async def scalars(statement):
        entity = statement.column_descriptions[0]["entity"]
        return SimpleNamespace(
            all=lambda: (
                events
                if entity is TaskEvent
                else runs
                if entity is admission.Run
                else writes
                if entity is WorkAdmissionAudit
                else [task.id]
            )
        )

    db.scalars.side_effect = scalars

    async def paid(command, session):
        result = await _audit(
            session,
            "paid_work",
            WorkAdmissionRead(outcome="denied", reason="engineering_budget_denied"),
            reference_id=command.id,
            command_payload=command.model_dump(mode="json"),
        )
        return PaidRunStartRead(admission=result)

    monkeypatch.setattr(admission, "start_paid_run", AsyncMock(side_effect=paid))
    return task, story, events, runs, db, writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,outcome",
    [
        ("engineering_budget_denied", "denied"),
        ("emergency_stop", "denied"),
        ("paid_work_limit", "deferred"),
    ],
)
@pytest.mark.parametrize("iteration", [0, 1])
async def test_no_run_refusal_disposes_task_story_and_both_notices(
    refusal, reason, outcome, iteration
):
    task, story, events, runs, db, writes = refusal
    task.current_iteration = iteration

    async def paid(command, session):
        result = await _audit(
            session,
            "paid_work",
            WorkAdmissionRead(outcome=outcome, reason=reason),
            reference_id=command.id,
            command_payload=command.model_dump(mode="json"),
        )
        return PaidRunStartRead(admission=result)

    admission.start_paid_run.side_effect = paid
    result = await admission.admit_engineering_dispatch(
        EngineeringDispatchCommand(task_id=task.id), db
    )
    assert task.status == story.status == "waiting_human_review"
    assert task.current_iteration == iteration and task.max_iterations == 3
    assert result.run_id is None
    assert result.refusal_disposition.task_id == task.id
    decision = result.refusal_disposition.decision_id
    assert writes[0].reference_id == decision and isinstance(writes[0], WorkAdmissionAudit)
    assert (
        task.id in story.quarantine_reason["detail"]
        and decision in story.quarantine_reason["detail"]
    )
    assert story.owner_notification["state"] == story.owner_notification["admin_state"] == "owed"
    snapshot = story.quarantine_reason, story.owner_notification, len(events), len(writes)
    assert (
        await admission.admit_engineering_dispatch(EngineeringDispatchCommand(task_id=task.id), db)
    ).reason.value == "task_not_dispatchable"
    assert (story.quarantine_reason, story.owner_notification, len(events), len(writes)) == snapshot
    assert not runs
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["cycle", "pr", "live", "admission", "foreign", "unrelated_review"]
)
@pytest.mark.parametrize("workspace_failed", [False, True])
async def test_stale_or_unrelated_evidence_never_reaches_paid_refusal(
    refusal, change, workspace_failed
):
    task, story, events, runs, db, writes = refusal
    if workspace_failed:
        from src.routers import projects_guards

        projects_guards.load_locked_project.return_value.config = {
            "scaffold_error": "ensure failed"
        }
    if change == "cycle":
        story.reopened_at = story.created_at + timedelta(seconds=1)
    elif change == "pr":
        story.pr_number += 1
    elif change == "live":
        from shared.models import Run

        runs.append(
            Run(
                id="live",
                task_id=task.id,
                status="running",
                created_at=story.created_at,
                run_metadata={"iteration": 0},
            )
        )
    elif change == "admission":
        events[0].details = {"pr_conflict_repair": {"malformed": True}}
    elif change == "foreign":
        story.project_id = uuid.uuid4()
    else:
        story.status = "waiting_human_review"
    before = task.status, task.current_iteration, story.status, len(events)
    if change in {"admission", "foreign"}:
        with pytest.raises(HTTPException):
            await admission.admit_engineering_dispatch(
                EngineeringDispatchCommand(task_id=task.id), db
            )
    else:
        await admission.admit_engineering_dispatch(EngineeringDispatchCommand(task_id=task.id), db)
    admission.start_paid_run.assert_not_awaited()
    assert (task.status, task.current_iteration, story.status, len(events)) == before
    assert not writes


def test_conflict_sibling_live_work_cannot_be_overridden_into_no_run_disposition(refusal):
    from shared.contracts.dto.engineering_dispatch import EngineeringDispatchRefusal
    from shared.models import Task

    task, _, _, _, _, _ = refusal
    sibling = Task(id="sibling", status="in_dev")
    command = EngineeringDispatchCommand(
        task_id=task.id, overrides=[EngineeringDispatchRefusal.STORY_BUSY]
    )
    result = admission._story_fence(
        task, {sibling.id}, {sibling.id: sibling}, [], admission._Overrides(command)
    )
    assert result.reason is EngineeringDispatchRefusal.STORY_BUSY


@pytest.mark.asyncio
async def test_native_resume_bound_is_admitted_without_rewriting_original_evidence(
    refusal, monkeypatch
):
    from src.routers import _task_actions

    task, story, events, runs, db, writes = refusal
    refuse_emergency_stop()
    await admission.admit_engineering_dispatch(EngineeringDispatchCommand(task_id=task.id), db)
    original = events[0].details.copy()
    monkeypatch.setattr(_task_actions, "get_task_for_update", AsyncMock(return_value=task))
    monkeypatch.setattr(_task_actions, "_get_story_for_update", AsyncMock(return_value=story))
    monkeypatch.setattr(_task_actions, "to_read", lambda row: row)
    await _task_actions.resume_task(
        task.id, _task_actions.TaskResume(guidance="capacity restored", retries=4), db=db
    )
    assert task.status == "todo" and story.status == "in_progress"
    assert task.current_iteration == 1 and task.max_iterations == 5
    # Deliberate resume is the only authority to replace the bound.
    result = await admission.admit_engineering_dispatch(
        EngineeringDispatchCommand(task_id=task.id), db
    )
    assert result.refusal_disposition is not None
    assert task.current_iteration == 1 and task.max_iterations == 5
    assert events[0].details == original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cycle", "pr", "quarantine", "bound"])
async def test_resume_refuses_stale_conflict_or_unrelated_stop(refusal, monkeypatch, change):
    from src.routers import _task_actions

    task, story, events, runs, db, writes = refusal
    await admission.admit_engineering_dispatch(EngineeringDispatchCommand(task_id=task.id), db)
    if change == "cycle":
        story.reopened_at = story.created_at + timedelta(seconds=1)
    elif change == "pr":
        story.pr_number += 1
    elif change == "bound":
        task.max_iterations += 1
    else:
        story.quarantine_reason = {"reason": "unrelated"}
    monkeypatch.setattr(_task_actions, "get_task_for_update", AsyncMock(return_value=task))
    monkeypatch.setattr(_task_actions, "_get_story_for_update", AsyncMock(return_value=story))
    monkeypatch.setattr(_task_actions, "to_read", lambda row: row)
    before = task.status, task.current_iteration, task.max_iterations, story.status, len(events)
    with pytest.raises(HTTPException):
        await _task_actions.resume_task(task.id, _task_actions.TaskResume(guidance="retry"), db=db)
    assert (
        task.status,
        task.current_iteration,
        task.max_iterations,
        story.status,
        len(events),
    ) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["transition", "reopen"])
@pytest.mark.parametrize("action", ["operator_resume", "budget_repair_readmitted"])
async def test_generic_body_cannot_forge_native_resume_authority(
    refusal, monkeypatch, route, action
):
    from src.routers import _task_actions

    task, story, events, _, db, _ = refusal
    task.status = "waiting_human_review"
    monkeypatch.setattr(_task_actions, "get_task_for_update", AsyncMock(return_value=task))
    monkeypatch.setattr(_task_actions, "to_read", lambda row: row)
    body = _task_actions.TaskTransition(actor="admin", details={"action": action})
    with pytest.raises(HTTPException) as error:
        if route == "transition":
            await _task_actions.transition_task(task.id, to_status="backlog", body=body, db=db)
        else:
            await _task_actions.reopen_task(task.id, body=body, db=db)
    assert error.value.status_code == 409 and task.status == "waiting_human_review"
    assert len(events) == 1
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_and_metadata_reset_cannot_borrow_refusal_recovery(refusal):
    task, story, _, _, db, writes = refusal
    await admission.admit_engineering_dispatch(EngineeringDispatchCommand(task_id=task.id), db)
    admission.start_paid_run.reset_mock()
    task.status = "todo"
    task.failure_metadata = None
    task.current_iteration += 1
    story.status = "in_progress"
    result = await admission.admit_engineering_dispatch(
        EngineeringDispatchCommand(task_id=task.id), db
    )
    assert result.reason.value == "task_not_dispatchable"
    admission.start_paid_run.assert_not_awaited()
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_older_iteration_cannot_borrow_native_resume(refusal, monkeypatch):
    from src.routers import _task_actions

    task, story, _, _, db, _ = refusal
    refuse_emergency_stop()
    await admission.admit_engineering_dispatch(EngineeringDispatchCommand(task_id=task.id), db)
    monkeypatch.setattr(_task_actions, "get_task_for_update", AsyncMock(return_value=task))
    monkeypatch.setattr(_task_actions, "_get_story_for_update", AsyncMock(return_value=story))
    monkeypatch.setattr(_task_actions, "to_read", lambda row: row)
    await _task_actions.resume_task(
        task.id, _task_actions.TaskResume(guidance="capacity", retries=4), db=db
    )
    admission.start_paid_run.reset_mock()
    task.current_iteration = 0
    with pytest.raises(HTTPException):
        await admission.admit_engineering_dispatch(EngineeringDispatchCommand(task_id=task.id), db)
    admission.start_paid_run.assert_not_awaited()
