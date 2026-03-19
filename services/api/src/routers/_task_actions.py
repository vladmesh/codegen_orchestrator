"""Task action endpoints — state machine transitions."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.task import TaskEventType, TaskStatus
from shared.contracts.queues.engineering import EngineeringMessage
from shared.models import Run, TaskEvent
from shared.queues import ENGINEERING_QUEUE
from shared.redis.client import RedisStreamClient

from ..database import get_async_session
from ..dependencies import get_redis_client
from ..schemas.actions import SpawnWorkerRequest
from ..schemas.run import RunRead
from ..schemas.task import TaskRead, TaskResume, TaskTransition
from ._task_helpers import create_status_event, get_task, to_read, validate_transition

logger = structlog.get_logger()

action_router = APIRouter()

# Path from working statuses to done (auto-promotion chain)
_COMPLETE_PATH: dict[str, list[str]] = {
    TaskStatus.IN_DEV: [TaskStatus.IN_CI, TaskStatus.TESTING, TaskStatus.DONE],
    TaskStatus.IN_CI: [TaskStatus.TESTING, TaskStatus.DONE],
    TaskStatus.TESTING: [TaskStatus.DONE],
}


@action_router.post("/{task_id}/start", response_model=TaskRead)
async def start_task(
    task_id: str,
    body: TaskTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> TaskRead:
    body = body or TaskTransition()
    task = await get_task(task_id, db)

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
    task = await get_task(task_id, db)

    path = _COMPLETE_PATH.get(task.status)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot complete task from status '{task.status}'",
        )

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
    task = await get_task(task_id, db)

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
    task = await get_task(task_id, db)

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
    task = await get_task(task_id, db)

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
    task = await get_task(task_id, db)

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
) -> dict:
    """Spawn an engineering worker for a task.

    Transitions task to IN_DEV (if not already), creates a Run,
    and publishes EngineeringMessage to engineering:queue.
    """
    body = body or SpawnWorkerRequest()
    task = await get_task(task_id, db)

    # Transition to IN_DEV if needed
    startable = {TaskStatus.BACKLOG, TaskStatus.TODO}
    if TaskStatus(task.status) in startable:
        if task.status == TaskStatus.BACKLOG:
            await create_status_event(task, TaskStatus.BACKLOG, TaskStatus.TODO, body.actor, {}, db)
            task.status = TaskStatus.TODO
        validate_transition(task.status, TaskStatus.IN_DEV)
        old_status = task.status
        task.status = TaskStatus.IN_DEV
        await create_status_event(task, old_status, TaskStatus.IN_DEV, body.actor, {}, db)
    elif task.status != TaskStatus.IN_DEV:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot spawn worker for task in status '{task.status}'",
        )

    # Create Run
    run_id = f"eng-{uuid.uuid4().hex[:12]}"
    run = Run(
        id=run_id,
        type="engineering",
        project_id=task.project_id,
        task_id=task.id,
        story_id=getattr(task, "story_id", None),
        run_metadata={
            "triggered_by": "admin",
            "task_id": task.id,
        },
    )
    db.add(run)
    await db.commit()
    await db.refresh(task)
    await db.refresh(run)

    # Publish to engineering queue
    msg = EngineeringMessage(
        task_id=run_id,
        project_id=str(task.project_id),
        user_id="",
        action=task.type or "feature",
        description=body.description or task.description,
        planning_task_id=task.id,
        story_id=getattr(task, "story_id", None) or None,
    )
    await redis.publish_message(ENGINEERING_QUEUE, msg)

    logger.info("worker_spawned", task_id=task.id, run_id=run_id, actor=body.actor)
    return {
        "task": to_read(task),
        "run": RunRead.model_validate(run, from_attributes=True),
    }
