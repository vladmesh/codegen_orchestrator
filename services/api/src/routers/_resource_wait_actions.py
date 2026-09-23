"""A task's resource wait, entered and left together with the owner notice it owes.

The liveness supervisor used to park a refused engineering task in three calls —
write the wait's facts, transition, publish the announcement — and resume it in
three more, with each announcement a best-effort publish after the commit. A
Redis or recipient failure there lost the message for good. Here each move is
one transaction on locked rows (Task, then Story, then Run, the repository's
ladder): the task's facts and hops, their status events, and the owed
``OwnerNotification`` on the engineering Run the move was decided on commit
together or not at all. Delivery then belongs to the owner-notification seam.

The record's facts are minted here from the locked rows, never taken from the
caller: the story status it is true in, the task statuses it is true in, and
``owed_at``. The caller supplies only the words.
"""

from datetime import UTC, datetime
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.lifecycle_wait import (
    RESOURCE_WAIT_EVENTS,
    RESOURCE_WAIT_TASK_STATUSES,
    RESOURCES_RESUMED_TASK_STATUSES,
    TaskResourceResumeCommand,
    TaskResourceResumeDisposition,
    TaskResourceResumeRead,
    TaskResourceWaitCommand,
    TaskResourceWaitDisposition,
    TaskResourceWaitRead,
)
from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.run import RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.vocab import OwnerNotificationEvent
from shared.models import Run, Task
from shared.models.story import Story

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ._story_helpers import _get_story_for_update
from ._task_helpers import create_status_event, get_task_for_update, validate_transition

logger = structlog.get_logger()

resource_wait_router = APIRouter()

#: The task event actions these moves record, next to their status events.
PARK_WAITING_RESOURCES_ACTION = "park_waiting_resources"
RESUME_FROM_RESOURCE_WAIT_ACTION = "resume_from_resource_wait"

#: The task's ``failure_metadata`` key that says a wait is under way. Its
#: presence is what makes a later park part of the same wait, announced once.
RESOURCE_WAIT_STARTED_KEY = "resource_wait_started_at"


def _conflict(code: str, message: str) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail={"code": code, "message": message}
    )


async def _locked_story(task: Task, db: AsyncSession) -> Story:
    """The task's story, locked. A resource-wait notice is always about a story task."""
    if task.story_id is None:
        _conflict("standalone_task", "A resource-wait notice needs the task's story.")
    return await _get_story_for_update(task.story_id, db)


async def _locked_engineering_run(task: Task, run_id: str, db: AsyncSession) -> Run:
    run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None or run.type != RunType.ENGINEERING.value or run.task_id != task.id:
        _conflict("stale_attempt_fence", "The named Run is not an engineering Run of this task.")
    return run


async def _locked_latest_engineering_run(task: Task, db: AsyncSession) -> Run:
    run = await db.scalar(
        select(Run)
        .where(Run.task_id == task.id, Run.type == RunType.ENGINEERING.value)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
        .with_for_update()
    )
    if run is None:
        # Only a refused engineering Run parks a task, so a waiting task without
        # one was not parked by this contract and has nowhere to owe a notice.
        _conflict("resource_wait_without_run", "The waiting task has no engineering Run.")
    return run


def _stored_record(run: Run) -> OwnerNotification | None:
    stored = (run.run_metadata or {}).get(OWNER_NOTIFICATION_KEY)
    return None if stored is None else OwnerNotification.model_validate(stored)


def _owe(
    run: Run,
    task: Task,
    story: Story,
    *,
    event: OwnerNotificationEvent,
    text: str,
    expected_task_statuses: tuple[TaskStatus, ...],
) -> OwnerNotification:
    """Write a fresh owed record on the Run, replacing whatever it carried.

    A replaced record — a wait's announcement the resume supersedes — keeps
    nothing: a visit still delivering it is refused its write by the newer
    ``owed_at`` (`OwnerNotification.supersedes`), and its own delivery check
    finds the task no longer waiting.
    """
    record = OwnerNotification(
        event=event,
        text=text,
        story_id=story.id,
        project_id=str(task.project_id),
        terminal_status=StoryStatus(story.status),
        task_id=task.id,
        expected_task_statuses=expected_task_statuses,
        state=OwnerNotificationState.OWED,
        owed_at=datetime.now(UTC),
    )
    run.run_metadata = {
        **(run.run_metadata or {}),
        OWNER_NOTIFICATION_KEY: record.model_dump(mode="json"),
    }
    return record


@resource_wait_router.post("/{task_id}/park-waiting-resources", response_model=TaskResourceWaitRead)
async def park_waiting_resources(
    task_id: str,
    command: TaskResourceWaitCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TaskResourceWaitRead:
    """Park a task whose engineering Run was refused placement, and owe the announcement.

    In one transaction: the wait's facts merge into ``failure_metadata`` (its
    start is kept across re-parks, so the timeout measures the whole wait), the
    task moves to ``waiting_resources`` with its status event, and — only when
    this park starts the wait — the owed announcement is written on the refused
    Run, true while the task is still waiting and the story is where it is now.
    A task already waiting is a repeat whose answer was lost: nothing is
    written, and the Run's current announcement is returned for delivery.
    """
    task = await get_task_for_update(task_id, db)
    story = await _locked_story(task, db)
    run = await _locked_engineering_run(task, command.run_id, db)

    if task.status == TaskStatus.WAITING_RESOURCES.value:
        record = _stored_record(run)
        return TaskResourceWaitRead(
            disposition=TaskResourceWaitDisposition.ALREADY_WAITING,
            task_id=task.id,
            run_id=run.id,
            task_status=TaskStatus.WAITING_RESOURCES,
            new_wait=False,
            owner_notification=(
                record if record is not None and record.event in RESOURCE_WAIT_EVENTS else None
            ),
        )
    validate_transition(task.status, TaskStatus.WAITING_RESOURCES)

    metadata = dict(task.failure_metadata or {})
    new_wait = RESOURCE_WAIT_STARTED_KEY not in metadata
    metadata.setdefault(RESOURCE_WAIT_STARTED_KEY, datetime.now(UTC).isoformat())
    metadata.update(
        {
            "allocation_required_ram_mb": command.allocation_required_ram_mb,
            "allocation_min_disk_mb": command.allocation_min_disk_mb,
            "allocation_failure_reason": command.allocation_failure_reason.value,
        }
    )
    task.failure_metadata = metadata
    from_status = task.status
    task.status = TaskStatus.WAITING_RESOURCES.value
    await create_status_event(
        task,
        from_status,
        TaskStatus.WAITING_RESOURCES,
        command.actor,
        {
            "action": PARK_WAITING_RESOURCES_ACTION,
            "attempt_id": run.id,
            "reason": command.allocation_failure_reason.value,
            "new_wait": new_wait,
        },
        db,
    )
    record = (
        _owe(
            run,
            task,
            story,
            event=command.event,
            text=command.text,
            expected_task_statuses=RESOURCE_WAIT_TASK_STATUSES,
        )
        if new_wait
        else None
    )
    await db.commit()
    logger.info(
        "task_parked_waiting_resources",
        task_id=task.id,
        story_id=story.id,
        run_id=run.id,
        reason=command.allocation_failure_reason.value,
        new_wait=new_wait,
    )
    return TaskResourceWaitRead(
        disposition=TaskResourceWaitDisposition.PARKED,
        task_id=task.id,
        run_id=run.id,
        task_status=TaskStatus.WAITING_RESOURCES,
        new_wait=new_wait,
        owner_notification=record,
    )


@resource_wait_router.post(
    "/{task_id}/resume-from-resource-wait", response_model=TaskResourceResumeRead
)
async def resume_from_resource_wait(
    task_id: str,
    command: TaskResourceResumeCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TaskResourceResumeRead:
    """Release a waiting task to the dispatcher, and owe the owner that it resumed.

    In one transaction the task goes ``waiting_resources`` → ``backlog`` →
    ``todo`` with both status events, and the "resumed" record replaces the
    wait's record on the task's latest engineering Run: true while the task is
    released or being worked on and the story is where it is now. A task that
    is no longer waiting is answered ``not_waiting`` and nothing is written —
    the move that took it out of the wait was not this one.
    """
    task = await get_task_for_update(task_id, db)
    if task.status != TaskStatus.WAITING_RESOURCES.value:
        return TaskResourceResumeRead(
            disposition=TaskResourceResumeDisposition.NOT_WAITING,
            task_id=task.id,
            task_status=TaskStatus(task.status),
        )
    story = await _locked_story(task, db)
    run = await _locked_latest_engineering_run(task, db)
    validate_transition(TaskStatus.WAITING_RESOURCES, TaskStatus.BACKLOG)
    validate_transition(TaskStatus.BACKLOG, TaskStatus.TODO)

    audit = {"action": RESUME_FROM_RESOURCE_WAIT_ACTION, "attempt_id": run.id}
    task.status = TaskStatus.BACKLOG.value
    await create_status_event(
        task, TaskStatus.WAITING_RESOURCES, TaskStatus.BACKLOG, command.actor, audit, db
    )
    task.status = TaskStatus.TODO.value
    await create_status_event(task, TaskStatus.BACKLOG, TaskStatus.TODO, command.actor, audit, db)
    record = _owe(
        run,
        task,
        story,
        event=OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
        text=command.text,
        expected_task_statuses=RESOURCES_RESUMED_TASK_STATUSES,
    )
    await db.commit()
    logger.info(
        "task_resumed_from_resource_wait", task_id=task.id, story_id=story.id, run_id=run.id
    )
    return TaskResourceResumeRead(
        disposition=TaskResourceResumeDisposition.RESUMED,
        task_id=task.id,
        task_status=TaskStatus.TODO,
        run_id=run.id,
        owner_notification=record,
    )
