"""Atomic count-based admission used by every paid-work entry point."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.work_admission import (
    PaidRunStartCommand,
    PaidRunStartRead,
    WorkAdmissionOutcome,
    WorkAdmissionRead,
    WorkAdmissionReason,
)
from shared.models import Project, Run, SystemConfig, User, WorkAdmissionAudit

EMERGENCY_STOP_KEY = "work_admission.emergency_stop"
MAX_PROJECTS_KEY = "work_admission.max_projects_per_user"
MAX_PAID_RUNS_KEY = "work_admission.max_concurrent_paid_runs"


async def _controls(db: AsyncSession, *keys: str) -> dict[str, object]:
    rows = (
        await db.scalars(select(SystemConfig).where(SystemConfig.key.in_(keys)).with_for_update())
    ).all()
    values = {row.key: row.value for row in rows}
    missing = set(keys) - values.keys()
    if missing:
        raise RuntimeError(f"Missing work admission config: {', '.join(sorted(missing))}")
    return values


async def _audit(
    db: AsyncSession,
    subject: str,
    decision: WorkAdmissionRead,
    *,
    user_id: int | None = None,
    reference_id: str | None = None,
) -> WorkAdmissionRead:
    db.add(
        WorkAdmissionAudit(
            subject=subject,
            outcome=decision.outcome.value,
            reason=decision.reason.value if decision.reason else None,
            user_id=user_id,
            reference_id=reference_id,
        )
    )
    return decision


def _limit(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{key} must be a non-negative integer")
    return value


async def _stop_or_continue(
    db: AsyncSession, subject: str, *, user_id: int | None = None, reference_id: str | None = None
) -> WorkAdmissionRead | None:
    controls = await _controls(db, EMERGENCY_STOP_KEY)
    enabled = controls[EMERGENCY_STOP_KEY]
    if not isinstance(enabled, bool):
        raise RuntimeError(f"{EMERGENCY_STOP_KEY} must be a boolean")
    if enabled:
        return await _audit(
            db,
            subject,
            WorkAdmissionRead(
                outcome=WorkAdmissionOutcome.DENIED,
                reason=WorkAdmissionReason.EMERGENCY_STOP,
            ),
            user_id=user_id,
            reference_id=reference_id,
        )
    return None


async def admit_project_creation(
    user_id: int, is_admin: bool, db: AsyncSession
) -> WorkAdmissionRead:
    """Lock a user's live-project count with the emergency-stop control."""
    await db.scalar(select(User).where(User.id == user_id).with_for_update())
    stopped = await _stop_or_continue(db, "project", user_id=user_id)
    if stopped is not None:
        return stopped
    if is_admin:
        return await _audit(
            db, "project", WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED), user_id=user_id
        )
    controls = await _controls(db, MAX_PROJECTS_KEY)
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(Project)
            .where(Project.owner_id == user_id, Project.status != ProjectStatus.ARCHIVED.value)
        )
        or 0
    )
    if count >= _limit(controls[MAX_PROJECTS_KEY], MAX_PROJECTS_KEY):
        return await _audit(
            db,
            "project",
            WorkAdmissionRead(
                outcome=WorkAdmissionOutcome.DENIED, reason=WorkAdmissionReason.PROJECT_LIMIT
            ),
            user_id=user_id,
        )
    return await _audit(
        db, "project", WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED), user_id=user_id
    )


async def start_paid_run(command: PaidRunStartCommand, db: AsyncSession) -> PaidRunStartRead:
    """Atomically decide and create one queued engineering or QA run.

    The config-row lock is retained through the Run INSERT and caller commit, so
    no successful decision can escape without occupying a counted slot.
    """
    stopped = await _stop_or_continue(db, "paid_work", reference_id=command.id)
    if stopped is not None:
        return PaidRunStartRead(admission=stopped)
    controls = await _controls(db, MAX_PAID_RUNS_KEY)
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(Run)
            .where(
                Run.type.in_((RunType.ENGINEERING.value, RunType.QA.value)),
                Run.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value)),
            )
        )
        or 0
    )
    if count >= _limit(controls[MAX_PAID_RUNS_KEY], MAX_PAID_RUNS_KEY):
        return PaidRunStartRead(
            admission=await _audit(
                db,
                "paid_work",
                WorkAdmissionRead(
                    outcome=WorkAdmissionOutcome.DEFERRED,
                    reason=WorkAdmissionReason.PAID_WORK_LIMIT,
                    retryable=True,
                ),
                reference_id=command.id,
            )
        )

    if command.type is RunType.ENGINEERING:
        from shared.contracts.dto.engineering_budget_policy import (
            EngineeringBudgetAdmissionCommand,
            EngineeringBudgetAdmissionOutcome,
        )

        from .engineering_budget_admission import admit_engineering_attempt

        budget = await admit_engineering_attempt(
            EngineeringBudgetAdmissionCommand(
                attempt_id=command.id,
                project_id=command.project_id,
                task_id=command.task_id,
                story_id=command.story_id,
            ),
            db,
        )
        if budget.outcome is EngineeringBudgetAdmissionOutcome.DENIED:
            return PaidRunStartRead(
                admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.DENIED)
            )

    run = Run(
        id=command.id,
        type=command.type.value,
        status=RunStatus.QUEUED.value,
        project_id=command.project_id,
        story_id=command.story_id,
        task_id=command.task_id,
        run_metadata=command.run_metadata,
        callback_stream=command.callback_stream,
    )
    db.add(run)
    admitted = await _audit(
        db,
        "paid_work",
        WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED),
        reference_id=command.id,
    )
    return PaidRunStartRead(admission=admitted, run_id=run.id)


async def admit_paid_work(run_id: str, db: AsyncSession) -> WorkAdmissionRead:
    """Admit an engineering or QA run; reaching the limit defers rather than fails it."""
    stopped = await _stop_or_continue(db, "paid_work", reference_id=run_id)
    if stopped is not None:
        return stopped
    controls = await _controls(db, MAX_PAID_RUNS_KEY)
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(Run)
            .where(
                Run.type.in_((RunType.ENGINEERING.value, RunType.QA.value)),
                Run.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value)),
            )
        )
        or 0
    )
    if count >= _limit(controls[MAX_PAID_RUNS_KEY], MAX_PAID_RUNS_KEY):
        return await _audit(
            db,
            "paid_work",
            WorkAdmissionRead(
                outcome=WorkAdmissionOutcome.DEFERRED,
                reason=WorkAdmissionReason.PAID_WORK_LIMIT,
                retryable=True,
            ),
            reference_id=run_id,
        )
    return await _audit(
        db,
        "paid_work",
        WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED),
        reference_id=run_id,
    )
