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
    EngineeringInfrastructureParkCommand,
    EngineeringInfrastructureParkDisposition,
    EngineeringInfrastructureParkRead,
    EngineeringInfrastructureRefusal,
    EngineeringInfrastructureRetryCommand,
    EngineeringInfrastructureRetryOutcome,
    EngineeringInfrastructureRetryRead,
    infrastructure_refusal_detail,
)
from shared.contracts.dto.owner_notification import OWNER_NOTIFICATION_KEY, OwnerNotification
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.run_result import DeployRunResult, EngineeringRunResult
from shared.contracts.dto.state_wait import (
    ANCHOR_RUN_TYPE_BY_STATUS,
    StateWaitExpiryCommand,
    StateWaitExpiryDisposition,
    StateWaitExpiryRead,
    StateWaitObservation,
)
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.work_admission import WorkAdmissionOutcome
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.models import Project, Run, Task, TaskEvent, WorkAdmissionAudit
from shared.models.story import Story

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..infrastructure_park import (
    SCAFFOLD_ERROR_KEY,
    WORKSPACE_ENSURE_AUDIT_SUBJECT,
    apply_infrastructure_park,
    infrastructure_conflict,
)
from ..schemas.story import StoryRead, StoryTransition
from ..work_admission import abort_paid_run_pre_handoff
from ._story_helpers import (
    _do_transition,
    _get_story_for_update,
    _land_on,
    _validate_transition,
)
from ._task_helpers import create_status_event, get_task_for_update, validate_transition
from .projects_guards import load_locked_project

logger = structlog.get_logger()

_LIVE_RUN_STATUSES = frozenset({RunStatus.QUEUED.value, RunStatus.RUNNING.value})

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

#: The paid gate's audit subject, and the outcomes it records for a refusal.
PAID_WORK_AUDIT_SUBJECT = "paid_work"
_REFUSED_AUDIT_OUTCOMES = frozenset(
    {WorkAdmissionOutcome.DENIED.value, WorkAdmissionOutcome.DEFERRED.value}
)

_infrastructure_conflict = infrastructure_conflict


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


async def _locked_refused_run(
    story_id: str,
    park: EngineeringInfrastructurePark,
    db: AsyncSession,
) -> Run | None:
    """Lock the refused Run, when one exists, and fail closed unless it matches."""
    run = await db.scalar(select(Run).where(Run.id == park.attempt_id).with_for_update())
    if run is None:
        # Admission-time executor refusals name the denied attempt but create no Run.
        return None
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
    return run


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
    # Ladder: Task, Story, then Project, then Run. A failed ensure-workspace is
    # recovered by letting ensure run again, which needs its recorded error gone.
    project = (
        await load_locked_project(db, task.project_id)
        if task_park.refusal is EngineeringInfrastructureRefusal.WORKSPACE_ENSURE_FAILED
        else None
    )
    refused_run = await _locked_refused_run(story.id, task_park, db)
    if refused_run is not None and refused_run.status in _LIVE_RUN_STATUSES:
        await abort_paid_run_pre_handoff(
            refused_run.id, "Recovered pre-agent infrastructure refusal", db
        )

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

    if project is not None and SCAFFOLD_ERROR_KEY in (project.config or {}):
        # Only the recorded failure goes; the scheduler's next tick runs ensure
        # again, and the task dispatches once `workspace_ready` is set.
        project_config = dict(project.config)
        project_config.pop(SCAFFOLD_ERROR_KEY)
        project.config = project_config

    await db.commit()
    return _retry_read(
        EngineeringInfrastructureRetryOutcome.RETRIED,
        story.id,
        command,
        task.current_iteration,
    )


def _park_read(
    disposition: EngineeringInfrastructureParkDisposition,
    story: Story,
    task: Task,
    park: EngineeringInfrastructurePark,
) -> EngineeringInfrastructureParkRead:
    return EngineeringInfrastructureParkRead(
        disposition=disposition,
        story_id=story.id,
        task_id=task.id,
        attempt_id=park.attempt_id,
        refusal=park.refusal,
        task_status=task.status,
        story_status=story.status,
        current_iteration=task.current_iteration,
    )


async def _prove_refusal(
    task: Task, story: Story, park: EngineeringInfrastructurePark, db: AsyncSession
) -> None:
    """Fail closed unless committed evidence proves exactly this park.

    A refused Run is the proof for a post-handoff refusal: it must match the task,
    story, typed refusal and the stable detail derived from it. Without a Run the
    only proof is the unique committed paid-work admission audit for the same
    task, story, current iteration, attempt id, typed reason and message. A
    syntactically valid command alone never quarantines a story.
    """
    if park.refusal is EngineeringInfrastructureRefusal.WORKSPACE_ENSURE_FAILED:
        await _prove_workspace_ensure_failure(task, story, park, db)
        return
    run = await _locked_refused_run(story.id, park, db)
    if run is not None:
        if park.detail != infrastructure_refusal_detail(park.refusal):
            _infrastructure_conflict(
                "stale_attempt_fence", "The park detail does not match the refused Run."
            )
        return
    await _prove_by_admission_audit(task, story, park, PAID_WORK_AUDIT_SUBJECT, db)


async def _prove_workspace_ensure_failure(
    task: Task, story: Story, park: EngineeringInfrastructurePark, db: AsyncSession
) -> None:
    """A failed ensure-workspace never has a Run: only admission's audit proves it.

    The audit subject is its own, so neither a paid-work audit nor a refused Run
    can prove this park, and this audit can prove no other refusal.
    """
    await _prove_by_admission_audit(task, story, park, WORKSPACE_ENSURE_AUDIT_SUBJECT, db)


async def _prove_by_admission_audit(
    task: Task,
    story: Story,
    park: EngineeringInfrastructurePark,
    subject: str,
    db: AsyncSession,
) -> None:
    """The unique committed admission audit of `subject` must match this park exactly."""
    audits = (
        await db.scalars(
            select(WorkAdmissionAudit)
            .where(
                WorkAdmissionAudit.subject == subject,
                WorkAdmissionAudit.reference_id == park.attempt_id,
            )
            .with_for_update()
        )
    ).all()
    if not audits:
        _infrastructure_conflict(
            "refusal_evidence_missing", "No refused Run or admission audit proves this park."
        )
    if len(audits) > 1:
        _infrastructure_conflict(
            "refusal_evidence_ambiguous", "More than one admission audit names this attempt."
        )
    audit = audits[0]
    payload = audit.command_payload or {}
    if (
        audit.outcome not in _REFUSED_AUDIT_OUTCOMES
        or audit.reason != park.refusal.value
        or audit.message != park.detail
        or payload.get("type") != RunType.ENGINEERING.value
        or payload.get("task_id") != task.id
        or payload.get("story_id") != story.id
        or (payload.get("run_metadata") or {}).get("iteration") != task.current_iteration
    ):
        _infrastructure_conflict(
            "stale_attempt_fence", "The admission audit does not match this park."
        )


@action_router.post(
    "/{story_id}/park-infrastructure-refusal",
    response_model=EngineeringInfrastructureParkRead,
)
async def park_infrastructure_refusal(
    story_id: str,
    command: EngineeringInfrastructureParkCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EngineeringInfrastructureParkRead:
    """Park one proven pre-agent refusal on its task and story in one transaction.

    The liveness supervisor's path for a refused Run. Admission parks its own
    refusals in the deciding transaction and never calls this. Evidence, legal
    task hops with audit events, the story transition, and both notification
    audiences commit together or not at all.
    """
    park = command.park
    # Keep the repository's lock ladder: Task before Story before Run.
    task = await get_task_for_update(park.task_id, db)
    story = await _get_story_for_update(story_id, db)
    if task.story_id != story.id:
        _infrastructure_conflict("wrong_task", "The task does not belong to this story.")
    await _prove_refusal(task, story, park, db)
    disposition = await apply_infrastructure_park(task, story, park, actor=command.actor, db=db)
    if disposition is EngineeringInfrastructureParkDisposition.PARKED:
        await db.commit()
    return _park_read(disposition, story, task, park)


def _missing_secrets_saved(run: Run, project: Project) -> bool:
    """Whether every secret the deploy Run reported missing is now on the project."""
    if run.result is None:
        return False
    missing = {
        secret.key for secret in DeployRunResult.model_validate(run.result).missing_user_secrets
    }
    # Names only: the stored values stay encrypted and are never read here.
    saved = set((project.config or {}).get("secrets") or {})
    return bool(missing) and missing <= saved


async def _observe_state_wait(
    story: Story, command: StateWaitExpiryCommand, db: AsyncSession
) -> StateWaitObservation:
    """What the locked rows say about the wait the command names.

    Lock ladder: the Story is already held; then the Project, whose row every
    secret write locks, then the latest Run of the anchor type.
    """
    observed = StateWaitObservation(status=StoryStatus(story.status), pr_number=story.pr_number)
    run_type = ANCHOR_RUN_TYPE_BY_STATUS.get(command.expected_status)
    if observed.status is not command.expected_status or run_type is None:
        return observed
    waits_on_secrets = command.expected_status is StoryStatus.WAITING_USER_SECRET
    project = await load_locked_project(db, story.project_id) if waits_on_secrets else None
    run = await db.scalar(
        select(Run)
        .where(Run.story_id == story.id, Run.type == run_type.value)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
        .with_for_update()
    )
    if run is None:
        return observed
    observed = observed.model_copy(update={"run_id": run.id, "run_status": RunStatus(run.status)})
    if project is None:
        return observed
    # Only a secret wait is anchored on the ask this Run carries.
    stored_ask = (run.run_metadata or {}).get(OWNER_NOTIFICATION_KEY)
    return observed.model_copy(
        update={
            "ask": None if stored_ask is None else OwnerNotification.model_validate(stored_ask),
            "secrets_saved": run.id == command.anchor.run_id
            and _missing_secrets_saved(run, project),
        }
    )


@action_router.post("/{story_id}/expire-state-wait", response_model=StateWaitExpiryRead)
async def expire_state_wait(
    story_id: str,
    command: StateWaitExpiryCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> StateWaitExpiryRead:
    """End one expired wait, only if the story is still where the watchdog saw it.

    The state-age watchdog's only way to park or fail a story. On the locked
    rows the status must be the one the watchdog read and the anchor the one its
    age was measured from (`StateWaitExpiryCommand.mismatch`); then the typed
    reason, the owed owner record and the transition commit together. Otherwise
    nothing is written and the mismatch is named. A repeat of an ending that
    already committed is `already_ended`, so it owes the owner nothing twice.
    """
    story = await _get_story_for_update(story_id, db)
    if command.owner_notification.story_id != story.id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The owner notification names another story.",
        )
    if command.is_repeat(story.status, story.quarantine_reason):
        return StateWaitExpiryRead(
            disposition=StateWaitExpiryDisposition.ALREADY_ENDED,
            story_id=story.id,
            story_status=StoryStatus(story.status),
        )
    skip = command.mismatch(await _observe_state_wait(story, command, db))
    if skip is not None:
        logger.info(
            "state_wait_expiry_skipped",
            story_id=story.id,
            expected_status=command.expected_status.value,
            story_status=story.status,
            mismatch=skip.reason.value,
            expected=skip.expected,
            actual=skip.actual,
        )
        return StateWaitExpiryRead(
            disposition=StateWaitExpiryDisposition.SKIPPED,
            story_id=story.id,
            story_status=StoryStatus(story.status),
            skip=skip,
        )
    story.quarantine_reason = command.reason.model_dump(mode="json")
    story.owner_notification = command.owner_notification.model_dump(mode="json")
    _do_transition(story, command.terminal_status)
    await db.commit()
    logger.info(
        "state_wait_expired",
        story_id=story.id,
        expected_status=command.expected_status.value,
        story_status=story.status,
        ending=command.ending.value,
    )
    return StateWaitExpiryRead(
        disposition=StateWaitExpiryDisposition.EXPIRED,
        story_id=story.id,
        story_status=command.terminal_status,
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
