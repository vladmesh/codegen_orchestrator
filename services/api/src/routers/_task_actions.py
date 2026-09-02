"""Task action endpoints — state machine transitions."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchCommand,
    EngineeringDispatchOrigin,
    EngineeringDispatchOutcome,
    EngineeringDispatchRefusal,
)
from shared.contracts.dto.task import TaskEventType, TaskStatus
from shared.contracts.queues.engineering import EngineeringMessage
from shared.models import Run, Task, TaskEvent
from shared.queues import ENGINEERING_QUEUE
from shared.redis.client import RedisStreamClient

from ..database import get_async_session
from ..dependencies import get_redis_client, require_internal_or_admin
from ..engineering_dispatch_admission import admit_engineering_dispatch
from ..schemas.actions import SpawnWorkerRequest
from ..schemas.run import RunRead
from ..schemas.task import TaskRead, TaskResume, TaskTransition
from ..work_admission import abort_paid_run_pre_handoff
from ._recipients import resolve_project_chat_id
from ._task_helpers import (
    create_status_event,
    get_task,
    get_task_for_update,
    to_read,
    validate_transition,
)

logger = structlog.get_logger()

action_router = APIRouter()

#: The conditions the operator spawn button is authorised to walk past, named
#: once here rather than being absent. `spawn-worker` exists to start a task a
#: human picked out — from backlog, or again on one already in_dev — so it
#: overrides the dispatchability status and the prior-attempt fence, which are
#: exactly the two conditions that describe "the scheduler would not have
#: started this now". Everything else — the internal project, an unresolved
#: blocker, a busy story, a draft or unprepared project, the budget and the
#: slot — refuses an operator spawn exactly as it refuses a scheduled one, and
#: the overrides that were used are recorded on the attempt.
_OPERATOR_SPAWN_OVERRIDES = [
    EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE,
    EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT,
]

#: Statuses this route will start a worker from. Its own transition validation,
#: not an admission condition: it says which hop the route is able to perform,
#: and it runs before admission so a status it cannot move consumes nothing.
_SPAWNABLE_FROM = {TaskStatus.BACKLOG, TaskStatus.TODO, TaskStatus.IN_DEV}


def _refusal_detail(value: str) -> str:
    """A typed refusal, as the one sentence an HTTP caller reads."""
    return f"Engineering dispatch refused: {value.replace('_', ' ')}"


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

    The operator's way in to the same admission point the dispatcher uses: this
    route decides nothing about whether the work may happen. It asks
    `admit_engineering_dispatch` with its two declared overrides and acts on the
    typed answer, so there is no way to publish an engineering message without
    passing the admission point.
    """
    body = body or SpawnWorkerRequest()

    # Unlocked, and only to reject a status this route could not move anyway:
    # admission takes the row locks, in its own order. Refusing here means no
    # reservation was consumed by a request that was never going to transition.
    peeked = await get_task(task_id, db)
    if TaskStatus(peeked.status) not in _SPAWNABLE_FROM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot spawn worker for task in status '{peeked.status}'",
        )

    decision = await admit_engineering_dispatch(
        EngineeringDispatchCommand(
            task_id=task_id,
            origin=EngineeringDispatchOrigin.ADMIN,
            overrides=_OPERATOR_SPAWN_OVERRIDES,
        ),
        db,
    )
    if decision.outcome is not EngineeringDispatchOutcome.ADMITTED:
        # Nothing to publish. The commit keeps whatever the paid gate recorded
        # about its own decision; no Run was left queued by a refusal.
        await db.commit()
        logger.info(
            "worker_spawn_admission_refused",
            task_id=task_id,
            outcome=decision.outcome.value,
            reason=decision.reason.value if decision.reason else None,
            repair=decision.repair.value if decision.repair else None,
        )
        if decision.paid_work is not None:
            # The paid gate's own refusal keeps the shape it has always had, so
            # a caller reading `admission` still reads the same document.
            return {"admission": decision.paid_work.admission.model_dump(mode="json")}
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_refusal_detail(
                decision.reason.value if decision.reason else decision.repair.value
            ),
        )

    run_id = decision.run_id
    task = await db.get(Task, task_id)
    if task is None:
        raise RuntimeError("Locked task disappeared before worker handoff")
    try:
        # The admitted row is the locked one, so this is the status the transition
        # is actually applied to — not the one the unlocked peek above saw.
        task_status = TaskStatus(task.status)
        if task_status is not TaskStatus.IN_DEV:
            if task_status is TaskStatus.BACKLOG:
                validate_transition(TaskStatus.BACKLOG, TaskStatus.TODO)
                await create_status_event(
                    task, TaskStatus.BACKLOG, TaskStatus.TODO, body.actor, {}, db
                )
                task.status = TaskStatus.TODO
            old_status = task.status
            validate_transition(old_status, TaskStatus.IN_DEV)
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
            initiating_run_id=decision.initiating_run_id,
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

    logger.info(
        "worker_spawned",
        task_id=task.id,
        run_id=run_id,
        actor=body.actor,
        overridden=[reason.value for reason in decision.overridden],
    )
    return {
        "task": to_read(task),
        "run": RunRead.model_validate(run, from_attributes=True),
    }
