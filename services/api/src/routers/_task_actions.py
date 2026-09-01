"""Task action endpoints — state machine transitions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.task import TaskEventType, TaskStatus
from shared.contracts.dto.work_admission import PaidRunStartCommand, WorkAdmissionOutcome
from shared.contracts.queues.engineering import EngineeringMessage
from shared.models import Project, Run, TaskEvent
from shared.queues import ENGINEERING_QUEUE
from shared.redis.client import RedisStreamClient

from ..database import get_async_session
from ..dependencies import get_redis_client, require_internal_or_admin
from ..schemas.actions import SpawnWorkerRequest
from ..schemas.run import RunRead
from ..schemas.task import TaskRead, TaskResume, TaskTransition
from ..work_admission import abort_paid_run_pre_handoff, start_paid_run
from ._ownership import initiating_run_or_conflict
from ._recipients import resolve_project_chat_id
from ._task_helpers import (
    create_status_event,
    get_task_for_update,
    to_read,
    validate_transition,
)

logger = structlog.get_logger()

action_router = APIRouter()

# Path from working statuses to done (auto-promotion chain)
_COMPLETE_PATH: dict[str, list[str]] = {
    TaskStatus.IN_DEV: [TaskStatus.IN_CI, TaskStatus.TESTING, TaskStatus.DONE],
    TaskStatus.IN_CI: [TaskStatus.TESTING, TaskStatus.DONE],
    TaskStatus.TESTING: [TaskStatus.DONE],
}


async def _release_pre_handoff_failure(run_id: str, db: AsyncSession) -> None:
    """Close the unpublished Run and release its hold in one transaction."""
    await db.rollback()
    try:
        await abort_paid_run_pre_handoff(run_id, "Engineering handoff preparation failed", db)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("worker_spawn_reservation_release_failed", run_id=run_id)


@action_router.post("/{task_id}/start", response_model=TaskRead)
async def start_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await get_task_for_update(task_id, db)

    # Allow start from backlog (auto-promote to todo first) or from todo
    if task.status == TaskStatus.BACKLOG:
        await create_status_event(task, TaskStatus.BACKLOG, TaskStatus.TODO, body.actor, {}, db)
        task.status = TaskStatus.TODO

    validate_transition(task.status, TaskStatus.IN_DEV)

    old_status = task.status
    task.status = TaskStatus.IN_DEV
    await create_status_event(task, old_status, TaskStatus.IN_DEV, body.actor, body.details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_started", task_id=task.id)
    return to_read(task)


@action_router.post("/{task_id}/complete", response_model=TaskRead)
async def complete_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await get_task_for_update(task_id, db)

    path = _COMPLETE_PATH.get(task.status)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot complete task from status '{task.status}'",
        )

    # Every hop of the shortcut is checked against VALID_TRANSITIONS before any
    # of it is applied, so an illegal step cannot leave the task half-promoted.
    cursor = task.status
    for next_status in path:
        validate_transition(cursor, next_status)
        cursor = next_status

    for next_status in path:
        old_status = task.status
        task.status = next_status
        await create_status_event(task, old_status, next_status, body.actor, body.details, db)

    await db.commit()
    await db.refresh(task)

    logger.info("task_completed", task_id=task.id)
    return to_read(task)


@action_router.post("/{task_id}/fail", response_model=TaskRead)
async def fail_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await get_task_for_update(task_id, db)

    validate_transition(task.status, TaskStatus.FAILED)

    old_status = task.status
    task.status = TaskStatus.FAILED
    details = body.details.copy()
    if body.reason:
        details["reason"] = body.reason
    await create_status_event(task, old_status, TaskStatus.FAILED, body.actor, details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_failed", task_id=task.id, reason=body.reason)
    return to_read(task)


@action_router.post("/{task_id}/reopen", response_model=TaskRead)
async def reopen_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await get_task_for_update(task_id, db)

    validate_transition(task.status, TaskStatus.BACKLOG)

    old_status = task.status
    task.status = TaskStatus.BACKLOG
    details = body.details.copy()
    if body.reason:
        details["reason"] = body.reason
    await create_status_event(task, old_status, TaskStatus.BACKLOG, body.actor, details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_reopened", task_id=task.id, reason=body.reason)
    return to_read(task)


@action_router.post("/{task_id}/resume", response_model=TaskRead)
async def resume_task(
    task_id: str,
    body: TaskResume,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    """Resume a task from WAITING_HUMAN_REVIEW with admin guidance.

    Transitions task WHR -> IN_DEV and creates a 'guidance' event
    containing the admin's instructions for the next worker attempt.
    """
    task = await get_task_for_update(task_id, db)

    validate_transition(task.status, TaskStatus.IN_DEV)

    old_status = task.status
    task.status = TaskStatus.IN_DEV
    await create_status_event(
        task, old_status, TaskStatus.IN_DEV, body.actor, {"guidance": body.guidance}, db
    )

    # Also create a guidance event for the worker to pick up
    event = TaskEvent(
        task_id=task.id,
        event_type=TaskEventType.NOTE.value,
        actor=body.actor,
        details={"action": "guidance", "guidance": body.guidance},
    )
    db.add(event)

    await db.commit()
    await db.refresh(task)

    logger.info("task_resumed", task_id=task.id, actor=body.actor)
    return to_read(task)


@action_router.post("/{task_id}/transition", response_model=TaskRead)
async def transition_task(
    task_id: str,
    to_status: str = Query(...),
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await get_task_for_update(task_id, db)

    validate_transition(task.status, to_status)

    old_status = task.status
    task.status = to_status
    await create_status_event(task, old_status, to_status, body.actor, body.details, db)
    await db.commit()
    await db.refresh(task)

    logger.info("task_transitioned", task_id=task.id, from_s=old_status, to_s=to_status)
    return to_read(task)


@action_router.post("/{task_id}/spawn-worker")
async def spawn_worker(
    task_id: str,
    body: SpawnWorkerRequest | None = None,
    db: AsyncSession = Depends(get_async_session),
    redis: RedisStreamClient = Depends(get_redis_client),
    _: None = Depends(require_internal_or_admin),
) -> dict:
    """Spawn an engineering worker for a task.

    Transitions task to IN_DEV (if not already), creates a Run,
    and publishes EngineeringMessage to engineering:queue.
    """
    body = body or SpawnWorkerRequest()
    task = await get_task_for_update(task_id, db)
    # The run this work belongs to, recorded when the project was created. The
    # message below cannot be built without it, so an admin-spawned worker is
    # owned on exactly the same terms as a dispatched one.
    project = await db.get(Project, task.project_id)
    if project is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {task.project_id} not found",
        )
    # Refused here, before the task is moved or a Run row exists: a project
    # that predates run ownership must not leave a half-started attempt behind.
    initiating_run_id = initiating_run_or_conflict(project)

    # Validate every local refusal before admission.  These checks do not modify
    # the task, so a bad status cannot consume a reservation.
    startable = {TaskStatus.BACKLOG, TaskStatus.TODO}
    task_status = TaskStatus(task.status)
    if task_status in startable:
        if task_status is TaskStatus.BACKLOG:
            validate_transition(TaskStatus.BACKLOG, TaskStatus.TODO)
            task_status = TaskStatus.TODO
        validate_transition(task_status, TaskStatus.IN_DEV)
    elif task_status is not TaskStatus.IN_DEV:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot spawn worker for task in status '{task.status}'",
        )

    run_id = f"eng-{uuid.uuid4().hex[:12]}"
    started = await start_paid_run(
        PaidRunStartCommand(
            id=run_id,
            type="engineering",
            project_id=task.project_id,
            task_id=task.id,
            story_id=getattr(task, "story_id", None),
            run_metadata={"triggered_by": "admin", "task_id": task.id},
        ),
        db,
    )
    if started.admission.outcome is not WorkAdmissionOutcome.ADMITTED:
        await db.commit()
        logger.info(
            "worker_spawn_count_admission_refused",
            task_id=task.id,
            run_id=run_id,
            reason=started.admission.reason.value if started.admission.reason else None,
        )
        return {"admission": started.admission.model_dump(mode="json")}

    try:
        # Transition to IN_DEV if needed.
        if TaskStatus(task.status) in startable:
            if task.status == TaskStatus.BACKLOG:
                await create_status_event(
                    task, TaskStatus.BACKLOG, TaskStatus.TODO, body.actor, {}, db
                )
                task.status = TaskStatus.TODO
            old_status = task.status
            task.status = TaskStatus.IN_DEV
            await create_status_event(task, old_status, TaskStatus.IN_DEV, body.actor, {}, db)

        await db.commit()
        run = await db.get(Run, run_id)
        if run is None:
            raise RuntimeError("Paid run disappeared before worker handoff")
        await db.refresh(task)
        await db.refresh(run)

        msg = EngineeringMessage(
            task_id=run_id,
            project_id=str(task.project_id),
            initiating_run_id=initiating_run_id,
            telegram_chat_id=await resolve_project_chat_id(
                db,
                task.project_id,
                event="worker_spawned",
                story_id=task.story_id or "",
            ),
            action=task.type or "feature",
            description=body.description or task.description,
            planning_task_id=task.id,
            story_id=getattr(task, "story_id", None) or None,
        )
    except Exception as error:
        # Everything here precedes the queue call.  A publish exception is not
        # proof that no worker received the message, so it is handled below.
        await _release_pre_handoff_failure(run_id, db)
        logger.exception(
            "worker_spawn_pre_handoff_preparation_failed", task_id=task_id, run_id=run_id
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Engineering handoff could not be published",
        ) from error
    try:
        await redis.publish_message(ENGINEERING_QUEUE, msg)
    except Exception as error:
        logger.exception("worker_spawn_publish_outcome_unknown", task_id=task_id, run_id=run_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Engineering handoff could not be confirmed",
        ) from error

    logger.info("worker_spawned", task_id=task.id, run_id=run_id, actor=body.actor)
    return {
        "task": to_read(task),
        "run": RunRead.model_validate(run, from_attributes=True),
    }
