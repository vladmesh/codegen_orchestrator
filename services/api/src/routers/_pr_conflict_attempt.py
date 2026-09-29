"""The admitted conflict Task's retry and terminal settlement transaction."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering import EngineeringStatus
from shared.contracts.dto.engineering_execution import EngineeringExecutionPhase
from shared.contracts.dto.owner_notification import OwnerNotification, OwnerNotificationState
from shared.contracts.dto.pr_conflict_repair import (
    PR_CONFLICT_REPAIR_ATTEMPT_KEY,
    PR_CONFLICT_REPAIR_KEY,
    PRConflictRepairAttemptCommand,
    PRConflictRepairAttemptDisposition,
    PRConflictRepairAttemptOutcome,
    PRConflictRepairAttemptRead,
    PRConflictRepairEvidence,
    cycle_stamp,
    repair_task_id,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringFailureReason, EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import (
    StoryFailure,
    StoryFailureCode,
    bounded_diagnostic,
    story_failure_admin_text,
    story_failure_owner_text,
)
from shared.contracts.dto.task import TaskEventType, TaskStatus, TaskType
from shared.models import Repository, Run, Story, Task, TaskEvent

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ._story_helpers import _do_transition, _get_story_for_update, _record_story_failure
from ._task_helpers import create_status_event, get_task_for_update, validate_transition
from .projects_guards import load_locked_project

router = APIRouter()


def _refuse(message: str):
    raise HTTPException(409, detail={"code": "pr_conflict_repair_refused", "message": message})


async def _settle_terminal(task, story, command, evidence, result, own_stop, audit, db):
    if task.status != TaskStatus.WAITING_HUMAN_REVIEW.value:
        validate_transition(task.status, TaskStatus.WAITING_HUMAN_REVIEW)
        before = task.status
        task.status = TaskStatus.WAITING_HUMAN_REVIEW.value
        await create_status_event(
            task, before, TaskStatus.WAITING_HUMAN_REVIEW, "internal_service", audit, db
        )
    if not own_stop:
        failure = StoryFailure(
            code=StoryFailureCode.NO_NEW_COMMIT
            if result.failure_reason is EngineeringFailureReason.NO_NEW_COMMIT
            else StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED,
            source="engineering"
            if command.disposition is PRConflictRepairAttemptDisposition.GAVE_UP
            else "scheduler",
            detail=(
                f"PR #{evidence.pr_number}: repair Task {task.id}, Run {command.attempt_id}, "
                f"iteration {command.expected_iteration}, ceiling {evidence.max_iterations}. "
                f"{command.detail} The one automatic repair Task ended without a usable result."
            ),
        )
        _record_story_failure(story, failure, StoryStatus.WAITING_HUMAN_REVIEW)
        _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
    task.failure_metadata = {**(task.failure_metadata or {}), "reason": command.detail}


async def _admission_evidence(
    task: Task,
    story: Story,
    events: list[TaskEvent],
    db: AsyncSession,
) -> PRConflictRepairEvidence:
    admissions = [
        e.details[PR_CONFLICT_REPAIR_KEY] for e in events if PR_CONFLICT_REPAIR_KEY in e.details
    ]
    if len(admissions) != 1:
        _refuse("The immutable repair admission is missing or ambiguous.")
    try:
        evidence = PRConflictRepairEvidence.model_validate(admissions[0])
    except ValidationError:
        _refuse("The immutable repair admission is malformed.")
    repository = await db.get(Repository, evidence.repository_id)
    if (
        task.id != repair_task_id(story.id, evidence.cycle_started_at)
        or task.created_by != PR_CONFLICT_REPAIR_KEY
        or task.type != TaskType.FIX.value
        or not task.dispatch_admitted
        or task.repository_id != evidence.repository_id
        or repository is None
        or repository.project_id != story.project_id
        or repository.role != "primary"
        or evidence.story_id != story.id
        or evidence.project_id != story.project_id
        or task.max_iterations != evidence.max_iterations
    ):
        _refuse("The repair admission does not prove this bounded Task.")
    return evidence


def _recorded_outcome(events, run_id, command):
    for event in events:
        settled = event.details.get(PR_CONFLICT_REPAIR_ATTEMPT_KEY)
        if settled and settled["attempt_id"] == run_id:
            if (
                settled["disposition"] != command.disposition.value
                or settled["iteration"] != command.expected_iteration
            ):
                return PRConflictRepairAttemptOutcome.STALE
            outcome = PRConflictRepairAttemptOutcome(settled["outcome"])
            return (
                PRConflictRepairAttemptOutcome.REUSED
                if outcome is PRConflictRepairAttemptOutcome.RETRIED
                else outcome
            )
    return None


def _verify_recorded_stop(story):
    failure = StoryFailure.model_validate(story.quarantine_reason)
    notice = OwnerNotification.model_validate(story.owner_notification)
    if (
        notice.text != story_failure_owner_text(failure)
        or notice.admin_text != story_failure_admin_text(story.id, str(story.project_id), failure)
        or notice.state is OwnerNotificationState.VOIDED
        or notice.admin_state in {None, OwnerNotificationState.VOIDED}
    ):
        _refuse("Recorded repair stop has inconsistent notice obligations.")


async def _retry_task(task, iteration, audit, db):
    if task.status == TaskStatus.IN_DEV.value:
        validate_transition(task.status, TaskStatus.FAILED)
        task.status = TaskStatus.FAILED.value
        await create_status_event(
            task, TaskStatus.IN_DEV, TaskStatus.FAILED, "internal_service", audit, db
        )
    if task.status == TaskStatus.FAILED.value:
        validate_transition(task.status, TaskStatus.BACKLOG)
        task.status = TaskStatus.BACKLOG.value
        await create_status_event(
            task, TaskStatus.FAILED, TaskStatus.BACKLOG, "internal_service", audit, db
        )
    validate_transition(task.status, TaskStatus.TODO)
    task.status = TaskStatus.TODO.value
    task.current_iteration = iteration + 1
    await create_status_event(
        task, TaskStatus.BACKLOG, TaskStatus.TODO, "internal_service", audit, db
    )


async def _start_interrupted_dispatch(task, audit, db):
    if task.status == TaskStatus.TODO.value:
        validate_transition(task.status, TaskStatus.IN_DEV)
        task.status = TaskStatus.IN_DEV.value
        await create_status_event(
            task, TaskStatus.TODO, TaskStatus.IN_DEV, "internal_service", audit, db
        )


def _terminal_command(run, command):
    try:
        result = EngineeringRunResult.model_validate(run.result)
    except ValidationError:
        _refuse("The terminal engineering result is missing or malformed.")
    if result.allocation_failure_reason is not None or (
        result.execution is not None
        and result.execution.execution_phase is EngineeringExecutionPhase.PRE_AGENT_REFUSED
    ):
        _refuse("Infrastructure and resource refusals require their native disposition.")
    if result.engineering_status not in {EngineeringStatus.GAVE_UP, EngineeringStatus.FAILED}:
        _refuse("The failed Run has no failed/gave_up engineering outcome.")
    # Immutable evidence, not callback-local observations, decides the result.
    return result, command.model_copy(
        update={
            "disposition": PRConflictRepairAttemptDisposition(result.engineering_status.value),
            "detail": bounded_diagnostic(run.error_message)
            if run.error_message
            else f"Engineering Run {run.id} recorded {result.engineering_status.value}.",
        }
    )


@router.post(
    "/{story_id}/repair-pr-conflicts/attempt-outcome", response_model=PRConflictRepairAttemptRead
)
async def settle_pr_conflict_attempt(
    story_id: str,
    command: PRConflictRepairAttemptCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> PRConflictRepairAttemptRead:
    task = await get_task_for_update(command.task_id, db)
    story = await _get_story_for_update(story_id, db)
    project = await load_locked_project(db, story.project_id)

    def read(outcome):
        return PRConflictRepairAttemptRead(
            outcome=outcome,
            task_id=task.id,
            attempt_id=command.attempt_id,
            current_iteration=task.current_iteration,
            task_status=task.status,
            story_status=story.status,
        )

    if (
        task.story_id != story.id
        or task.project_id != project.id
        or command.project_id != project.id
    ):
        _refuse("The repair Task or command belongs to another project/story.")
    if (
        cycle_stamp(command.cycle_started_at) != cycle_stamp(story.reopened_at or story.created_at)
        or command.pr_number != story.pr_number
    ):
        return read(PRConflictRepairAttemptOutcome.STALE)
    events = (
        await db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
        )
    ).all()
    evidence = await _admission_evidence(task, story, events, db)
    if evidence.pr_number != command.pr_number or cycle_stamp(
        evidence.cycle_started_at
    ) != cycle_stamp(command.cycle_started_at):
        _refuse("The repair admission does not prove this bounded Task.")
    runs = (
        await db.scalars(
            select(Run).where(Run.story_id == story.id).order_by(Run.id).with_for_update()
        )
    ).all()
    run = next((r for r in runs if r.id == command.attempt_id), None)
    if run is None:
        return read(PRConflictRepairAttemptOutcome.STALE)
    if (
        run.task_id != task.id
        or run.project_id != project.id
        or run.type != RunType.ENGINEERING.value
    ):
        _refuse("The Run does not belong to this repair Task.")
    iteration = (run.run_metadata or {}).get("iteration")
    if type(iteration) is not int:
        _refuse("The Run has no valid iteration identity.")
    if iteration != command.expected_iteration:
        return read(PRConflictRepairAttemptOutcome.STALE)
    if run.status != RunStatus.FAILED.value:
        return read(PRConflictRepairAttemptOutcome.STALE)
    result, command = _terminal_command(run, command)
    # The ledger is immutable and belongs to this Run. Replays stay harmless
    # even after the next admitted Run has started or finished.
    recorded = _recorded_outcome(events, run.id, command)
    if recorded is not None:
        return read(recorded)
    if (
        iteration != task.current_iteration
        or any(r.status in {RunStatus.QUEUED.value, RunStatus.RUNNING.value} for r in runs)
        or any(
            r.task_id == task.id
            and r.id != run.id
            and type((r.run_metadata or {}).get("iteration")) is int
            and r.run_metadata["iteration"] >= iteration
            for r in runs
        )
    ):
        return read(PRConflictRepairAttemptOutcome.STALE)
    own_stop = (
        story.status == StoryStatus.WAITING_HUMAN_REVIEW.value
        and (story.quarantine_reason or {}).get("code")
        in {
            StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED.value,
            StoryFailureCode.NO_NEW_COMMIT.value
            if result.failure_reason is EngineeringFailureReason.NO_NEW_COMMIT
            else StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED.value,
        }
        and task.id in (story.quarantine_reason or {}).get("detail", "")
    )
    if (
        story.status not in {StoryStatus.IN_PROGRESS.value, StoryStatus.PR_REVIEW.value}
        and not own_stop
    ):
        return read(PRConflictRepairAttemptOutcome.STALE)
    if task.status == TaskStatus.BACKLOG.value:
        _refuse("No native conflict producer leaves a partial BACKLOG retry.")
    if task.status not in {
        TaskStatus.TODO.value,
        TaskStatus.IN_DEV.value,
        TaskStatus.FAILED.value,
        TaskStatus.WAITING_HUMAN_REVIEW.value if own_stop else TaskStatus.IN_DEV.value,
    }:
        return read(PRConflictRepairAttemptOutcome.STALE)
    if own_stop:
        _verify_recorded_stop(story)
    audit = {"attempt_id": run.id, "iteration": iteration, "disposition": command.disposition.value}
    terminal = (
        own_stop
        or command.disposition is PRConflictRepairAttemptDisposition.GAVE_UP
        or iteration >= evidence.max_iterations
    )
    if not terminal and story.status != StoryStatus.IN_PROGRESS.value:
        return read(PRConflictRepairAttemptOutcome.STALE)
    await _start_interrupted_dispatch(task, audit, db)
    if terminal:
        await _settle_terminal(task, story, command, evidence, result, own_stop, audit, db)
        outcome = PRConflictRepairAttemptOutcome.EXHAUSTED
    else:
        await _retry_task(task, iteration, audit, db)
        outcome = PRConflictRepairAttemptOutcome.RETRIED
    db.add(
        TaskEvent(
            task_id=task.id,
            event_type=TaskEventType.NOTE.value,
            actor="internal_service",
            details={PR_CONFLICT_REPAIR_ATTEMPT_KEY: {**audit, "outcome": outcome.value}},
        )
    )
    await db.commit()
    return read(outcome)
