"""Composite Story actions — the one place a multi-hop Story move is declared.

A composite move used to be a sequence of `POST /stories/{id}/{action}` calls
issued by a poller or a consumer: a crash or a 422 between two of them left the
story parked in an intermediate status with nobody to finish it. Here the whole
move is one endpoint call — one locked row, one transaction, every hop checked
against ``VALID_TRANSITIONS`` before any hop is applied — so clients report the
event that happened and never sequence lifecycle state themselves.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.engineering_execution import (
    ENGINEERING_INFRASTRUCTURE_KEY,
    EngineeringExecutionPhase,
    EngineeringInfrastructurePark,
    EngineeringInfrastructureRetryCommand,
    EngineeringInfrastructureRetryOutcome,
    EngineeringInfrastructureRetryRead,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import EngineeringRunResult
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.models import Run, TaskEvent
from shared.models.story import Story

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..schemas.story import StoryRead, StoryTransition
from ..work_admission import abort_paid_run_pre_handoff
from ._story_helpers import _get_story_for_update, _land_on, _validate_transition
from ._task_helpers import create_status_event, get_task_for_update, validate_transition

logger = structlog.get_logger()

action_router = APIRouter()

#: The CI-failure retry: the PR poller has recorded the failed CI run and
#: created the fix task, so the story records the failed attempt, opens a new
#: work cycle and goes back to engineering.  Was three client calls
#: (`fail` → `reopen` → `start`) in `pr_poller._record_ci_failure`.
RETRY_AFTER_CI_FAILURE = "retry-after-ci-failure"

#: Every composite Story move the platform performs, as the ordered chain of
#: hops it applies.  Nothing outside this table walks a Story through more than
#: one status; a new composite is a new entry here plus its endpoint below.
COMPOSITE_CHAINS: dict[str, tuple[StoryStatus, ...]] = {
    RETRY_AFTER_CI_FAILURE: (
        StoryStatus.FAILED,
        StoryStatus.REOPENED,
        StoryStatus.IN_PROGRESS,
    ),
}

INFRASTRUCTURE_RETRY_ACTION = "retry_infrastructure_attempt"


def _infrastructure_conflict(code: str, message: str) -> None:
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "message": message},
    )


def _park_from_metadata(metadata: dict | None) -> EngineeringInfrastructurePark | None:
    if not isinstance(metadata, dict) or ENGINEERING_INFRASTRUCTURE_KEY not in metadata:
        return None
    try:
        return EngineeringInfrastructurePark.model_validate(
            metadata[ENGINEERING_INFRASTRUCTURE_KEY]
        )
    except ValueError:
        return None


async def _was_infrastructure_retry_recorded(
    task_id: str, command: EngineeringInfrastructureRetryCommand, db: AsyncSession
) -> bool:
    events = (
        await db.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id.desc())
        )
    ).all()
    expected = {
        "action": INFRASTRUCTURE_RETRY_ACTION,
        "attempt_id": command.attempt_id,
        "refusal": command.refusal.value,
    }
    return any(
        all((event.details or {}).get(key) == value for key, value in expected.items())
        for event in events
    )


def _retry_read(
    outcome: EngineeringInfrastructureRetryOutcome,
    story_id: str,
    command: EngineeringInfrastructureRetryCommand,
    current_iteration: int,
) -> EngineeringInfrastructureRetryRead:
    return EngineeringInfrastructureRetryRead(
        outcome=outcome,
        story_id=story_id,
        task_id=command.task_id,
        attempt_id=command.attempt_id,
        refusal=command.refusal,
        current_iteration=current_iteration,
    )


async def _settle_refused_run(
    story_id: str,
    park: EngineeringInfrastructurePark,
    db: AsyncSession,
) -> None:
    run = await db.scalar(select(Run).where(Run.id == park.attempt_id).with_for_update())
    if run is None:
        # Admission-time executor refusals name the denied attempt but create no Run.
        return
    if (
        run.type != RunType.ENGINEERING.value
        or run.task_id != park.task_id
        or run.story_id != story_id
    ):
        _infrastructure_conflict(
            "stale_attempt_fence", "The refused Run no longer matches this story and task."
        )
    try:
        execution = (
            EngineeringRunResult.model_validate(run.result).execution
            if run.result is not None
            else AttemptTurnMetadata.from_run_metadata(run.run_metadata).execution
        )
    except ValueError:
        execution = None
    if (
        execution is None
        or execution.execution_phase is not EngineeringExecutionPhase.PRE_AGENT_REFUSED
        or execution.infrastructure_refusal is not park.refusal
    ):
        _infrastructure_conflict(
            "stale_attempt_fence", "The refused Run has no matching pre-agent evidence."
        )
    if run.status in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
        await abort_paid_run_pre_handoff(run.id, "Recovered pre-agent infrastructure refusal", db)


@action_router.post(
    "/{story_id}/retry-infrastructure-attempt",
    response_model=EngineeringInfrastructureRetryRead,
)
async def retry_infrastructure_attempt(
    story_id: str,
    command: EngineeringInfrastructureRetryCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EngineeringInfrastructureRetryRead:
    """Reset exactly one typed pre-agent refusal and restart its story atomically."""
    # Keep the repository's lock ladder: Task before Story before Run.
    task = await get_task_for_update(command.task_id, db)
    story = await _get_story_for_update(story_id, db)
    if task.story_id != story.id:
        _infrastructure_conflict("wrong_task", "The task does not belong to this story.")

    if await _was_infrastructure_retry_recorded(task.id, command, db):
        if task.status == TaskStatus.TODO.value and story.status == StoryStatus.IN_PROGRESS.value:
            return _retry_read(
                EngineeringInfrastructureRetryOutcome.ALREADY_RETRIED,
                story_id,
                command,
                task.current_iteration,
            )
        _infrastructure_conflict(
            "retry_state_changed", "The recorded retry no longer has its fresh-attempt state."
        )

    task_park = _park_from_metadata(task.failure_metadata)
    story_park = _park_from_metadata(story.quarantine_reason)
    if task_park is None or story_park is None:
        _infrastructure_conflict(
            "not_infrastructure_park", "Story and task are not parked by infrastructure."
        )
    if task_park != story_park or any(
        (
            task_park.task_id != command.task_id,
            task_park.attempt_id != command.attempt_id,
            task_park.refusal is not command.refusal,
        )
    ):
        _infrastructure_conflict(
            "stale_infrastructure_reason", "The recorded infrastructure refusal changed."
        )
    if (
        task.status != TaskStatus.WAITING_HUMAN_REVIEW.value
        or story.status != StoryStatus.WAITING_HUMAN_REVIEW.value
    ):
        _infrastructure_conflict(
            "wrong_status", "Infrastructure retry requires story and task in human review."
        )

    validate_transition(task.status, TaskStatus.BACKLOG)
    validate_transition(TaskStatus.BACKLOG, TaskStatus.TODO)
    _validate_transition(story.status, StoryStatus.IN_PROGRESS.value)
    await _settle_refused_run(story.id, task_park, db)

    audit = {
        "action": INFRASTRUCTURE_RETRY_ACTION,
        "attempt_id": command.attempt_id,
        "refusal": command.refusal.value,
    }
    task.status = TaskStatus.BACKLOG.value
    await create_status_event(
        task,
        TaskStatus.WAITING_HUMAN_REVIEW,
        TaskStatus.BACKLOG,
        command.actor,
        audit,
        db,
    )
    task.status = TaskStatus.TODO.value
    await create_status_event(task, TaskStatus.BACKLOG, TaskStatus.TODO, command.actor, audit, db)
    task_metadata = dict(task.failure_metadata or {})
    task_metadata.pop(ENGINEERING_INFRASTRUCTURE_KEY)
    task.failure_metadata = task_metadata or None

    story_metadata = dict(story.quarantine_reason or {})
    story_metadata.pop(ENGINEERING_INFRASTRUCTURE_KEY)
    story.quarantine_reason = story_metadata or None
    _land_on(story, StoryStatus.IN_PROGRESS)

    await db.commit()
    return _retry_read(
        EngineeringInfrastructureRetryOutcome.RETRIED,
        story.id,
        command,
        task.current_iteration,
    )


def _apply_chain(story: Story, chain: tuple[StoryStatus, ...]) -> None:
    """Validate every hop of the chain against VALID_TRANSITIONS, then apply it.

    The validation pass writes nothing, so an illegal hop anywhere in the chain
    raises 422 with the story exactly as it was.  Partial application is
    impossible: the first write happens only once the whole chain is known to
    be legal, and all of them commit together with the caller's transaction.

    Only the status the chain lands on survives, and ``waiting_on`` lands with
    it: every hop writes both through ``_land_on``, so the committed row carries
    the wait its final status implies.
    """
    cursor = story.status
    for hop in chain:
        _validate_transition(cursor, hop.value)
        cursor = hop.value

    for hop in chain:
        # The same landing write the single-hop path uses, so a composite gets
        # `waiting_on` from the one mapping rather than a copy of it.
        _land_on(story, hop)
        if hop is StoryStatus.REOPENED:
            # Reopening starts the current work cycle, and completion reads
            # this stamp to refuse pre-reopen QA evidence.  A composite that
            # passes through REOPENED writes it exactly as the single hop does.
            story.reopened_at = datetime.now(UTC)


@action_router.post(f"/{{story_id}}/{RETRY_AFTER_CI_FAILURE}", response_model=StoryRead)
async def retry_story_after_ci_failure(
    story_id: str,
    body: StoryTransition | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> StoryRead:
    """Send a story whose CI run failed back to engineering in one move.

    failed → reopened → in_progress, applied on one locked row.  The caller has
    already created the fix task; it reports the CI failure and nothing else.
    """
    body = body or StoryTransition()
    story = await _get_story_for_update(story_id, db)

    _apply_chain(story, COMPOSITE_CHAINS[RETRY_AFTER_CI_FAILURE])

    await db.commit()
    await db.refresh(story)

    logger.info("story_retried_after_ci_failure", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)
