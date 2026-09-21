"""Task action endpoints — state machine transitions."""

from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchCommand,
    EngineeringDispatchOrigin,
    EngineeringDispatchOutcome,
    EngineeringDispatchRefusal,
)
from shared.contracts.dto.engineering_execution import ENGINEERING_INFRASTRUCTURE_KEY
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
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
from ._story_helpers import (
    _get_story_for_update,
    _land_on,
    _validate_transition as _validate_story_transition,
)
from ._task_helpers import (
    create_status_event,
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


#: The operator's fresh attempt, as its task events name it.
RESUME_ACTION = "operator_resume"

#: Story statuses a resumed task's story may be in: parked with it, or already
#: back in progress because a sibling was resumed first.
_RESUMABLE_STORY_STATUSES = frozenset(
    {StoryStatus.WAITING_HUMAN_REVIEW.value, StoryStatus.IN_PROGRESS.value}
)

#: Statuses of a run whose worker may still hold the story branch.
_LIVE_RUN_STATUSES = frozenset({RunStatus.QUEUED.value, RunStatus.RUNNING.value})


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


def _refuse_resume(reason: str, message: str) -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"reason": reason, "message": message},
    )


def _fresh_iteration(task: Task, runs: list[Run]) -> int:
    """The first iteration no engineering run of this task has ever carried.

    An engineering attempt is identified by its iteration: the dispatcher's
    replay rule (`_prior_attempt` in admission) applies a finished run whose
    iteration equals the task's current one, because a task a failed transition
    left in todo owes that outcome. Resuming onto a number past every existing
    run is what makes the operator's attempt a new one rather than that case —
    the replay rule has nothing of this iteration to apply, and it keeps firing
    for the case it exists for.
    """
    stamped = [
        iteration
        for run in runs
        if isinstance(iteration := (run.run_metadata or {}).get("iteration"), int)
    ]
    return max([task.current_iteration, *stamped]) + 1


@action_router.post("/{task_id}/resume", response_model=TaskRead)
async def resume_task(
    task_id: str,
    body: TaskResume,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> TaskRead:
    """Give a task parked in waiting_human_review one fresh engineering attempt.

    The operator's single retry path. In one transaction, on locked rows:

    - the task goes WHR → backlog → todo on a fresh iteration, so the
      dispatcher's next tick admits and creates a new run instead of replaying
      an earlier attempt's outcome;
    - `max_iterations` is set to that iteration plus `body.retries`, so the
      retry budget is granted deliberately and recorded, not inferred from
      `current_iteration` overshooting it;
    - its story leaves waiting_human_review for in_progress, so the pipeline
      resumes with the task;
    - the parked attempts' `failure_metadata` moves onto the audit record;
    - the operator's guidance is recorded as a note.

    Refused, with a reason, for a task that is not parked, one parked by a typed
    infrastructure refusal (its own retry clears that evidence), one that still
    has a live run, or one whose story branch another task's worker holds.
    """
    # Ladder: Task, then Story, then Run — the order admission and every other
    # task/story writer take them.
    task = await get_task_for_update(task_id, db)
    if task.status != TaskStatus.WAITING_HUMAN_REVIEW.value:
        # 422 like every other hop this router cannot perform from a status.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "reason": "task_not_parked",
                "message": (
                    f"The task is '{task.status}'; only a task parked in "
                    f"'{TaskStatus.WAITING_HUMAN_REVIEW.value}' gets a fresh attempt."
                ),
            },
        )
    if ENGINEERING_INFRASTRUCTURE_KEY in (task.failure_metadata or {}):
        _refuse_resume(
            "infrastructure_parked",
            "The task is parked by a typed infrastructure refusal; "
            "POST /stories/{story_id}/retry-infrastructure-attempt clears it.",
        )

    story = await _get_story_for_update(task.story_id, db) if task.story_id else None
    if story is not None and story.status not in _RESUMABLE_STORY_STATUSES:
        _refuse_resume(
            "story_not_resumable",
            f"The story is '{story.status}'; only a story in human review or in progress "
            "takes a resumed task.",
        )

    siblings: dict[str, str] = {}
    if story is not None:
        # Column-only: the siblings' statuses are read, never materialised. Any
        # admission that could mint a run for one of them holds this task's row
        # too (the whole roster is its first rung), so none commits while the
        # lock above is held.
        siblings = dict(
            (
                await db.execute(
                    select(Task.id, Task.status).where(
                        Task.story_id == story.id, Task.id != task.id
                    )
                )
            ).all()
        )
    runs = list(
        (
            await db.scalars(
                select(Run)
                .where(
                    Run.task_id.in_([task.id, *siblings]),
                    Run.type == RunType.ENGINEERING.value,
                )
                .order_by(Run.id)
                .with_for_update()
            )
        ).all()
    )
    live = [
        run
        for run in runs
        if run.status in _LIVE_RUN_STATUSES
        and not (run.run_metadata or {}).get("pre_handoff_aborted")
    ]
    if any(run.task_id == task.id for run in live):
        _refuse_resume(
            EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT.value,
            "The task still has a live engineering run; it is not parked.",
        )
    if live or TaskStatus.IN_DEV.value in siblings.values():
        _refuse_resume(
            EngineeringDispatchRefusal.STORY_BUSY.value,
            "Another task of this story holds the story branch with a live worker.",
        )

    iteration = _fresh_iteration(task, [run for run in runs if run.task_id == task.id])
    audit = {
        "action": RESUME_ACTION,
        "previous_iteration": task.current_iteration,
        "previous_max_iterations": task.max_iterations,
        "iteration": iteration,
        "max_iterations": iteration + body.retries,
        "retries": body.retries,
        # What the parked attempts left behind — a gave-up reason, a resource
        # wait's start — belongs to them: kept here, and gone from the task, so
        # nothing reads it as the fresh attempt's own.
        "previous_failure_metadata": task.failure_metadata,
    }
    validate_transition(task.status, TaskStatus.BACKLOG)
    validate_transition(TaskStatus.BACKLOG, TaskStatus.TODO)
    if story is not None and story.status != StoryStatus.IN_PROGRESS.value:
        _validate_story_transition(story.status, StoryStatus.IN_PROGRESS.value)

    task.status = TaskStatus.BACKLOG.value
    await create_status_event(
        task, TaskStatus.WAITING_HUMAN_REVIEW, TaskStatus.BACKLOG, body.actor, audit, db
    )
    task.status = TaskStatus.TODO.value
    await create_status_event(task, TaskStatus.BACKLOG, TaskStatus.TODO, body.actor, audit, db)
    task.current_iteration = iteration
    task.max_iterations = iteration + body.retries
    task.failure_metadata = None
    db.add(
        TaskEvent(
            task_id=task.id,
            event_type=TaskEventType.NOTE.value,
            actor=body.actor,
            iteration=iteration,
            details={"action": "guidance", "guidance": body.guidance},
        )
    )
    if story is not None and story.status != StoryStatus.IN_PROGRESS.value:
        _land_on(story, StoryStatus.IN_PROGRESS)

    await db.commit()
    await db.refresh(task)

    logger.info(
        "task_resumed",
        task_id=task.id,
        story_id=task.story_id,
        actor=body.actor,
        iteration=iteration,
        max_iterations=task.max_iterations,
    )
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

    # Column-only, unlocked, and only to reject a status this route could not
    # move anyway: admission takes the row locks, in its own order. Refusing here
    # means no reservation was consumed by a request that was never going to
    # transition.
    #
    # It must not be `get_task`. Materialising the Task here would put the entity
    # in this session, and SQLAlchemy's identity map then hands admission's
    # `SELECT ... FOR UPDATE` that same object with its already-loaded
    # attributes: the conditions would be decided on the status this read saw
    # rather than on the locked one, and the transition below would write over
    # whatever committed in between. A column-only read materialises nothing, so
    # the locking read that follows is the only view of the row this request has.
    peeked_status = await db.scalar(select(Task.status).where(Task.id == task_id))
    if peeked_status is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found"
        )
    if TaskStatus(peeked_status) not in _SPAWNABLE_FROM:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Cannot spawn worker for task in status '{peeked_status}'",
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
