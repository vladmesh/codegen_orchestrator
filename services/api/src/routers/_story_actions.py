"""Composite Story actions — the one place a multi-hop Story move is declared.

A composite move used to be a sequence of `POST /stories/{id}/{action}` calls
issued by a poller or a consumer: a crash or a 422 between two of them left the
story parked in an intermediate status with nobody to finish it. Here the whole
move is one endpoint call — one locked row, one transaction, every hop checked
against ``VALID_TRANSITIONS`` before any hop is applied — so clients report the
event that happened and never sequence lifecycle state themselves.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetAdmissionOutcome
from shared.contracts.dto.engineering_dispatch import (
    ENGINEERING_DISPATCH_REFUSAL_KEY,
    EngineeringDispatchRefusal,
    EngineeringDispatchRefusalDisposition,
)
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
from shared.contracts.dto.lifecycle_wait import (
    UserSecretWaitCommand,
    UserSecretWaitDisposition,
    UserSecretWaitRead,
)
from shared.contracts.dto.owner_notification import (
    OWNER_NOTIFICATION_KEY,
    OwnerNotification,
    OwnerNotificationState,
)
from shared.contracts.dto.pr_conflict_repair import (
    PR_CONFLICT_REPAIR_KEY,
    PRConflictRepairCommand,
    PRConflictRepairEvidence,
    PRConflictRepairOutcome,
    PRConflictRepairRead,
    cycle_stamp,
    repair_task_id,
)
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
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode, in_work_cycle
from shared.contracts.dto.task import TaskEventType, TaskStatus, TaskType
from shared.contracts.dto.work_admission import WorkAdmissionOutcome
from shared.contracts.vocab import OwnerNotificationEvent
from shared.contracts.worker_turn import AttemptTurnMetadata
from shared.models import (
    EngineeringBudgetReservation,
    Project,
    Repository,
    Run,
    SystemConfig,
    Task,
    TaskEvent,
    WorkAdmissionAudit,
)
from shared.models.story import Story

from ..database import get_async_session
from ..dependencies import _optional_bearer_scheme, is_internal_service, require_internal_or_admin
from ..engineering_budget_admission import engineering_budget_has_capacity
from ..infrastructure_park import (
    SCAFFOLD_ERROR_KEY,
    WORKSPACE_ENSURE_AUDIT_SUBJECT,
    apply_infrastructure_park,
    infrastructure_conflict,
)
from ..owner_notification_settlement import preserve_po_settlement
from ..schemas.story import StoryRead, StoryTransition
from ..work_admission import abort_paid_run_pre_handoff
from ._pr_conflict_attempt import router as pr_conflict_attempt_router
from ._story_helpers import (
    _do_transition,
    _get_story_for_update,
    _land_on,
    _record_story_failure,
    _validate_transition,
    work_cycle_task_count,
)
from ._task_helpers import create_status_event, get_task_for_update, validate_transition
from .projects_guards import check_project_access, load_locked_project

logger = structlog.get_logger()

_LIVE_RUN_STATUSES = frozenset({RunStatus.QUEUED.value, RunStatus.RUNNING.value})

action_router = APIRouter()
action_router.include_router(pr_conflict_attempt_router)
PR_CONFLICT_GITHUB = GitHubAppClient


def _repair_conflict(message: str) -> None:
    raise HTTPException(409, detail={"code": "pr_conflict_repair_refused", "message": message})


@action_router.post("/{story_id}/repair-pr-conflicts", response_model=PRConflictRepairRead)
async def repair_pr_conflicts(
    story_id: str,
    command: PRConflictRepairCommand,
    db: AsyncSession = Depends(get_async_session),
    internal: bool = Depends(is_internal_service),
    telegram_id: int | None = Header(None, alias="X-Telegram-ID"),
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer_scheme),
) -> PRConflictRepairRead:
    # Existing Tasks precede Story, Project and Runs in the lock ladder. The
    # deterministic new Task is inserted only while the Story lock is held.
    tasks = (
        await db.scalars(
            select(Task).where(Task.story_id == story_id).order_by(Task.id).with_for_update()
        )
    ).all()
    story = await _get_story_for_update(story_id, db)
    project = await load_locked_project(db, story.project_id)
    actor = await check_project_access(
        project, telegram_id, db, is_internal=internal, credentials=credentials
    )
    cycle = cycle_stamp(story.reopened_at or story.created_at)
    if (
        command.project_id != story.project_id
        or command.pr_number != story.pr_number
        or cycle_stamp(command.cycle_started_at) != cycle
    ):
        _repair_conflict("Project, current PR or story cycle changed.")
    tid = repair_task_id(story.id, cycle)
    # A concurrent initial caller may have inserted it after our Task query.
    # Read it after acquiring Story; repeats never mutate or lock that Task.
    task = next((row for row in tasks if row.id == tid), None)
    if task is None:
        task = await db.get(Task, tid)
    reason = story.quarantine_reason or {}
    released_park = (
        story.status == StoryStatus.WAITING_HUMAN_REVIEW.value
        and reason.get("reason") == "github_app_merge_refused"
        and reason.get("mergeable_state") == "dirty"
        and reason.get("pr_number") == story.pr_number
    )
    exhausted_park = (
        task is not None
        and story.status == StoryStatus.WAITING_HUMAN_REVIEW.value
        and reason.get("code") == StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED.value
    )
    budget_park = _is_budget_park(task, story, reason)
    _validate_repair_story_status(story, task, released_park, exhausted_park, budget_park)
    if command.expected_head_sha is None and not (released_park or task is not None):
        _repair_conflict("Automatic admission requires the observed PR head.")
    current = [row for row in tasks if in_work_cycle(row.created_at, story.reopened_at, row.status)]
    if any(row.project_id != story.project_id for row in current):
        _repair_conflict("A story Task belongs to another project.")
    if task is None and (
        not any(row.status == TaskStatus.DONE.value for row in current)
        or any(
            row.status not in {TaskStatus.DONE.value, TaskStatus.CANCELLED.value} for row in current
        )
    ):
        _repair_conflict("Original engineering work is not settled.")
    live = (
        await db.scalars(
            select(Run)
            .where(Run.story_id == story.id, Run.status.in_(_LIVE_RUN_STATUSES))
            .order_by(Run.id)
            .with_for_update()
        )
    ).all()
    if any(task is None or run.task_id != task.id for run in live):
        _repair_conflict("A different attempt is live on this story.")
    repository = await db.scalar(
        select(Repository).where(
            Repository.project_id == story.project_id, Repository.role == "primary"
        )
    )
    if repository is None:
        _repair_conflict("The project has no primary repository.")
    head_sha, default, default_sha = await _observe_dirty_pr(story, repository, command)
    identity = "internal_service" if actor is None else f"user:{actor.id}"
    if task is None:
        control = await db.get(SystemConfig, "llm.task_default_max_iterations")
        if control is None or type(control.value) is not int or control.value <= 0:
            raise HTTPException(503, detail="Required engineering iteration bound is missing")
        evidence = PRConflictRepairEvidence(
            **command.model_dump(),
            story_id=story.id,
            repository_id=repository.id,
            head_sha=head_sha,
            default_branch=default,
            default_sha=default_sha,
            max_iterations=control.value,
        )
        task = Task(
            id=tid,
            project_id=story.project_id,
            story_id=story.id,
            repository_id=repository.id,
            type=TaskType.FIX.value,
            status=TaskStatus.TODO.value,
            title=f"Resolve conflicts in PR #{story.pr_number}",
            description=(
                f"Repair existing PR #{story.pr_number} on story/{story.id}. "
                f"Observed head {head_sha}; merge origin/{default} ({default_sha}) into "
                "this branch, resolve conflicts preserving both story and default work, "
                "run the product checks, commit and push normally. Preserve this PR. "
                "Do not force-push, reset, rebase or discard user work."
            ),
            max_iterations=evidence.max_iterations,
            created_by=PR_CONFLICT_REPAIR_KEY,
            dispatch_admitted=True,
            failure_metadata={PR_CONFLICT_REPAIR_KEY: evidence.model_dump(mode="json")},
        )
        db.add(task)
        await db.flush()
        db.add(
            TaskEvent(
                task_id=tid,
                event_type=TaskEventType.NOTE.value,
                actor=identity,
                details={
                    PR_CONFLICT_REPAIR_KEY: evidence.model_dump(mode="json"),
                    "previous_quarantine": reason if released_park else None,
                },
            )
        )
        _do_transition(story, StoryStatus.IN_PROGRESS)
        # Preserve the released refusal as immutable task admission evidence.
        if released_park:
            task.failure_metadata = {**task.failure_metadata, "previous_quarantine": reason}
            story.quarantine_reason = None
        await db.commit()
        outcome = PRConflictRepairOutcome.ADMITTED
    else:
        evidence = await _repair_admission_evidence(task, story, repository.id, db)
        budget_decision = await _budget_wait_decision(task, story, reason, db)
        if budget_decision is not None:
            await _readmit_budget_wait(task, story, project, budget_decision, identity, db)
            outcome = PRConflictRepairOutcome.REUSED
        elif exhausted_park:
            outcome = PRConflictRepairOutcome.EXHAUSTED
        elif not live and (
            task.status
            in {
                TaskStatus.DONE.value,
                TaskStatus.CANCELLED.value,
                TaskStatus.WAITING_HUMAN_REVIEW.value,
            }
            or (
                task.status == TaskStatus.FAILED.value
                and task.current_iteration >= evidence.max_iterations
            )
        ):
            failure = StoryFailure(
                code=StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED,
                source="scheduler",
                detail=(
                    f"PR #{story.pr_number} is still dirty at "
                    f"{head_sha}; repair Task {tid} ended {task.status} at iteration "
                    f"{task.current_iteration}. One repair Task is allowed, with iteration "
                    f"ceiling {evidence.max_iterations}; automatic repair allowance exhausted."
                ),
            )
            _record_story_failure(story, failure, StoryStatus.WAITING_HUMAN_REVIEW)
            if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value:
                _do_transition(story, StoryStatus.WAITING_HUMAN_REVIEW)
            await db.commit()
            outcome = PRConflictRepairOutcome.EXHAUSTED
        else:
            if story.status != StoryStatus.IN_PROGRESS.value:
                _repair_conflict("A pending repair has inconsistent story state.")
            outcome = PRConflictRepairOutcome.REUSED
    return PRConflictRepairRead(
        outcome=outcome,
        story_id=story.id,
        task_id=tid,
        pr_number=story.pr_number,
        max_iterations=evidence.max_iterations,
        reason=(story.quarantine_reason or {}).get("detail")
        if outcome is PRConflictRepairOutcome.EXHAUSTED
        or _is_budget_park(task, story, story.quarantine_reason or {})
        else None,
    )


async def _repair_admission_evidence(
    task: Task, story: Story, repository_id: str, db: AsyncSession
) -> PRConflictRepairEvidence:
    if task.story_id != story.id or task.project_id != story.project_id:
        _repair_conflict("The repair Task has inconsistent resource relationships.")
    from ._pr_conflict_attempt import _admission_evidence

    events = list(
        (
            await db.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
            )
        ).all()
    )
    evidence = await _admission_evidence(task, story, events, db)
    if (
        evidence.pr_number != story.pr_number
        or evidence.repository_id != repository_id
        or evidence.story_id != story.id
        or evidence.project_id != story.project_id
        or cycle_stamp(evidence.cycle_started_at)
        != cycle_stamp(story.reopened_at or story.created_at)
    ):
        _repair_conflict("The repair belongs to a replaced PR, repository or cycle.")
    return evidence


def _is_budget_park(task: Task | None, story: Story, reason: dict) -> bool:
    return bool(
        task is not None
        and story.status == StoryStatus.WAITING_HUMAN_REVIEW.value
        and reason.get("reason") == EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED.value
    )


def _validate_repair_story_status(
    story: Story,
    task: Task | None,
    released_park: bool,
    exhausted_park: bool,
    budget_park: bool,
) -> None:
    if story.status not in {StoryStatus.PR_REVIEW.value, StoryStatus.IN_PROGRESS.value}:
        if not (released_park or exhausted_park or budget_park):
            _repair_conflict("This human-review reason does not permit conflict recovery.")
    if task is None and story.status == StoryStatus.IN_PROGRESS.value:
        _repair_conflict("The story has engineering work in progress.")


async def _readmit_budget_wait(
    task: Task,
    story: Story,
    project: Project,
    decision_id: str,
    actor: str,
    db: AsyncSession,
) -> None:
    if not await engineering_budget_has_capacity(project.owner_id, db):
        return
    from ._pr_conflict_attempt import BUDGET_REPAIR_READMITTED_ACTION

    audit = {
        "action": BUDGET_REPAIR_READMITTED_ACTION,
        "decision_id": decision_id,
        "iteration": task.current_iteration,
    }
    validate_transition(task.status, TaskStatus.BACKLOG)
    task.status = TaskStatus.BACKLOG.value
    await create_status_event(
        task, TaskStatus.WAITING_HUMAN_REVIEW, TaskStatus.BACKLOG, actor, audit, db
    )
    validate_transition(task.status, TaskStatus.TODO)
    task.status = TaskStatus.TODO.value
    await create_status_event(task, TaskStatus.BACKLOG, TaskStatus.TODO, actor, audit, db)
    story.quarantine_reason = None
    _do_transition(story, StoryStatus.IN_PROGRESS)
    story.owner_notification = preserve_po_settlement(
        story.owner_notification,
        OwnerNotification(
            event=OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
            text=(
                f"Engineering budget is available; repair Task {task.id} "
                f"is queued for PR #{story.pr_number}."
            ),
            story_id=story.id,
            project_id=str(story.project_id),
            terminal_status=StoryStatus.IN_PROGRESS,
            task_id=task.id,
            expected_task_statuses=(TaskStatus.TODO, TaskStatus.IN_DEV),
            state=OwnerNotificationState.OWED,
            owed_at=datetime.now(UTC),
            admin_text=f"Repair Task {task.id} readmitted after budget decision {decision_id}.",
            admin_state=OwnerNotificationState.OWED,
        ),
    ).model_dump(mode="json")
    await db.commit()


async def _budget_wait_decision(
    task: Task, story: Story, reason: dict, db: AsyncSession
) -> str | None:
    """Prove a current no-Run budget wait from native, durable admission facts."""
    from ._pr_conflict_attempt import _pending_dispatch_refusal, _verify_recorded_stop

    if (
        task.status != TaskStatus.WAITING_HUMAN_REVIEW.value
        or story.status != StoryStatus.WAITING_HUMAN_REVIEW.value
        or reason.get("reason")
        not in {EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED.value, "story_failure"}
    ):
        return None
    events = list(
        (
            await db.scalars(
                select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
            )
        ).all()
    )
    saved = _pending_dispatch_refusal(events)
    if saved is None:
        return None
    try:
        refusal = EngineeringDispatchRefusalDisposition.model_validate(saved)
    except ValueError:
        _repair_conflict("The recorded budget refusal is malformed.")
    if refusal.reason != EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED:
        return None
    if refusal.task_id != task.id:
        _repair_conflict("The budget refusal belongs to another Task.")
    status_events = [e for e in events if e.event_type == TaskEventType.STATUS_CHANGE.value]
    if (
        not status_events
        or status_events[-1].to_status != TaskStatus.WAITING_HUMAN_REVIEW.value
        or status_events[-1].details.get(ENGINEERING_DISPATCH_REFUSAL_KEY) != saved
    ):
        return None
    if reason.get("reason") == EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED.value:
        if reason.get("task_id") != task.id or reason.get("decision_id") != refusal.decision_id:
            _repair_conflict("The budget wait does not match its admission decision.")
    elif (
        reason.get("code") != StoryFailureCode.PR_CONFLICT_REPAIR_EXHAUSTED.value
        or task.current_iteration != 0
        or task.id not in reason.get("detail", "")
        or refusal.decision_id not in reason.get("detail", "")
    ):
        return None
    if reason.get("reason") == "story_failure":
        _verify_recorded_stop(story)
    audit = await db.scalar(
        select(WorkAdmissionAudit).where(
            WorkAdmissionAudit.reference_id == refusal.decision_id,
            WorkAdmissionAudit.subject == "paid_work",
            WorkAdmissionAudit.reason == EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED.value,
        )
    )
    reservation = await db.scalar(
        select(EngineeringBudgetReservation).where(
            EngineeringBudgetReservation.attempt_id == refusal.decision_id
        )
    )
    if (
        audit is None
        or audit.outcome != "denied"
        or (audit.command_payload or {}).get("task_id") != task.id
        or (audit.command_payload or {}).get("story_id") != story.id
        or reservation is None
        or reservation.outcome != EngineeringBudgetAdmissionOutcome.DENIED
        or reservation.task_id != task.id
        or reservation.story_id != story.id
        or reservation.project_id != story.project_id
        or reservation.state is not None
    ):
        _repair_conflict("The budget refusal lacks its matching paid decision.")
    runs = (
        await db.scalars(
            select(Run).where(Run.task_id == task.id).order_by(Run.id).with_for_update()
        )
    ).all()
    if any((run.run_metadata or {}).get("iteration") == task.current_iteration for run in runs):
        return None
    if reason.get("reason") == "story_failure" and runs:
        return None
    return refusal.decision_id


async def _observe_dirty_pr(
    story: Story, repository: Repository, command: PRConflictRepairCommand
) -> tuple[str, str, str]:
    from .applications import _parse_github_repo_url

    owner, repo = _parse_github_repo_url(repository.git_url)
    try:
        async with PR_CONFLICT_GITHUB() as github:
            pr = await github.get_pull_request(owner, repo, command.pr_number)
            default = (await github.get_repo(owner, repo)).default_branch
            default_sha = await github.get_ref_sha(owner, repo, f"heads/{default}")
            current_head = await github.get_ref_sha(owner, repo, f"heads/story/{story.id}")
    except Exception as exc:
        logger.warning(
            "pr_conflict_observation_failed", story_id=story.id, error_type=type(exc).__name__
        )
        raise HTTPException(503, detail="Current PR evidence could not be read") from exc
    head, base = pr.get("head") or {}, pr.get("base") or {}
    full_name = f"{owner}/{repo}"
    if (
        pr.get("number") != story.pr_number
        or pr.get("state") != "open"
        or pr.get("merged_at")
        or pr.get("mergeable_state") != "dirty"
        or head.get("ref") != f"story/{story.id}"
        or (head.get("repo") or {}).get("full_name") != full_name
        or (base.get("repo") or {}).get("full_name") != full_name
        or base.get("ref") != default
        or base.get("sha") != default_sha
        or not default_sha
        or not current_head
        or head.get("sha") != current_head
        or (command.expected_head_sha is not None and current_head != command.expected_head_sha)
    ):
        _repair_conflict("The current open dirty PR does not match this story and default.")
    return current_head, default, default_sha


#: The CI-failure retry: the PR poller has recorded the failed CI run and
#: created the fix task, so the story records the failed attempt, opens a new
#: work cycle and goes back to engineering.  Was three client calls
#: (`fail` → `reopen` → `start`) in `pr_poller._record_ci_failure`.
RETRY_AFTER_CI_FAILURE = "retry-after-ci-failure"

#: Parking a story whose planning failed before the architect ever moved it to
#: in_progress — a reopen, which the architect starts only once it has planned,
#: or a story whose start was refused. `waiting_human_review` is reachable from
#: in_progress only, so the park passes through it on the same locked row.
#: Applied by `POST /stories/{id}/planning-outcome`, not an endpoint of its own.
PARK_UNSTARTED_PLANNING_FAILURE = "park-unstarted-planning-failure"

#: Every composite Story move the platform performs, as the ordered chain of
#: hops it applies.  Nothing outside this table walks a Story through more than
#: one status; a new composite is a new entry here plus its endpoint below.
COMPOSITE_CHAINS: dict[str, tuple[StoryStatus, ...]] = {
    RETRY_AFTER_CI_FAILURE: (
        StoryStatus.FAILED,
        StoryStatus.REOPENED,
        StoryStatus.IN_PROGRESS,
    ),
    PARK_UNSTARTED_PLANNING_FAILURE: (
        StoryStatus.IN_PROGRESS,
        StoryStatus.WAITING_HUMAN_REVIEW,
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


@action_router.post(
    "/{story_id}/park-waiting-user-secret",
    response_model=UserSecretWaitRead,
)
async def park_waiting_user_secret(
    story_id: str,
    command: UserSecretWaitCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> UserSecretWaitRead:
    """Park a deploying story on a missing user secret and owe the owner the ask.

    The deploy supervisor's path for a deploy Run that reported missing secrets.
    On the locked Story and then that Run, the ``waiting_user_secret`` transition
    and the owed ask on the Run commit together or not at all, so a story never
    waits on an ask nobody owes, and an ask is never owed for a wait that did
    not start. The ask is ``story_waiting_user_secret``, true while the story is
    in ``waiting_user_secret``; its ``delivered_at`` is what the state-age
    watchdog measures the wait from, and nothing here sets it.

    An ask already on the Run is kept, not replaced: this Run's wait is asked
    for once, and a delivery in flight is never reset. A story already waiting
    is a repeat whose answer was lost: nothing is written, and the Run's ask is
    returned for delivery.
    """
    story = await _get_story_for_update(story_id, db)
    run = await db.scalar(select(Run).where(Run.id == command.run_id).with_for_update())
    if run is None or run.type != RunType.DEPLOY.value or run.story_id != story.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "stale_attempt_fence",
                "message": "The named Run is not a deploy Run of this story.",
            },
        )
    stored = (run.run_metadata or {}).get(OWNER_NOTIFICATION_KEY)
    existing = None if stored is None else OwnerNotification.model_validate(stored)
    if story.status == StoryStatus.WAITING_USER_SECRET.value:
        return UserSecretWaitRead(
            disposition=UserSecretWaitDisposition.ALREADY_WAITING,
            story_id=story.id,
            story_status=StoryStatus.WAITING_USER_SECRET,
            run_id=run.id,
            owner_notification=existing,
        )
    _do_transition(story, StoryStatus.WAITING_USER_SECRET)
    ask = existing
    if existing is None or existing.state is OwnerNotificationState.VOIDED:
        ask = OwnerNotification(
            event=OwnerNotificationEvent.STORY_WAITING_USER_SECRET,
            text=command.text,
            story_id=story.id,
            project_id=str(story.project_id),
            terminal_status=StoryStatus.WAITING_USER_SECRET,
            state=OwnerNotificationState.OWED,
            owed_at=datetime.now(UTC),
        )
        run.run_metadata = {
            **(run.run_metadata or {}),
            OWNER_NOTIFICATION_KEY: preserve_po_settlement(stored, ask).model_dump(mode="json"),
        }
    await db.commit()
    logger.info(
        "story_waiting_user_secret",
        story_id=story.id,
        run_id=run.id,
        actor=command.actor,
        ask_state=ask.state.value,
    )
    return UserSecretWaitRead(
        disposition=UserSecretWaitDisposition.WAITING,
        story_id=story.id,
        story_status=StoryStatus.WAITING_USER_SECRET,
        run_id=run.id,
        owner_notification=ask,
    )


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


async def _work_cycle_task_count(story: Story, db: AsyncSession) -> int:
    return await work_cycle_task_count(story, db)


async def _observe_state_wait(
    story: Story, command: StateWaitExpiryCommand, db: AsyncSession
) -> StateWaitObservation:
    """What the locked rows say about the wait the command names.

    Lock ladder: the Story is already held; then the Project, whose row every
    secret write locks, then the latest Run of the anchor type.
    """
    observed = StateWaitObservation(
        status=StoryStatus(story.status),
        pr_number=story.pr_number,
        story_updated_at=story.updated_at,
    )
    if (
        observed.status is StoryStatus.IN_PROGRESS
        and command.expected_status is StoryStatus.IN_PROGRESS
    ):
        return observed.model_copy(
            update={"work_cycle_tasks": await _work_cycle_task_count(story, db)}
        )
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
    story.owner_notification = preserve_po_settlement(
        story.owner_notification, command.owner_notification
    ).model_dump(mode="json")
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

    original_cycle = story.reopened_at
    repair = await db.get(Task, repair_task_id(story.id, story.reopened_at or story.created_at))
    _apply_chain(story, COMPOSITE_CHAINS[RETRY_AFTER_CI_FAILURE])
    if repair is not None:
        # CI failure inside conflict repair is the same work cycle. A new stamp
        # would give a later dirty observation a second repair admission.
        story.reopened_at = original_cycle

    await db.commit()
    await db.refresh(story)

    logger.info("story_retried_after_ci_failure", story_id=story.id, actor=body.actor)
    return StoryRead.model_validate(story, from_attributes=True)
