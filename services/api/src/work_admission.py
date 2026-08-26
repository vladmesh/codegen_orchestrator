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

_REFUSAL_MESSAGES = {
    WorkAdmissionReason.EMERGENCY_STOP: "Запуск новой работы временно остановлен оператором.",
    WorkAdmissionReason.PROJECT_LIMIT: "Достигнут лимит активных проектов. Попробуйте позже.",
    WorkAdmissionReason.PAID_WORK_LIMIT: (
        "Достигнут лимит одновременной платной работы. Попробуйте позже."
    ),
    WorkAdmissionReason.ENGINEERING_BUDGET_DENIED: (
        "Недостаточно доступного бюджета для запуска инженерной задачи."
    ),
}


class PaidRunCommandConflict(Exception):
    """A stable paid-run id was replayed with different immutable input."""


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
    command_payload: dict | None = None,
) -> WorkAdmissionRead:
    if decision.reason is not None and decision.message is None:
        decision = decision.model_copy(update={"message": _REFUSAL_MESSAGES[decision.reason]})
    db.add(
        WorkAdmissionAudit(
            subject=subject,
            outcome=decision.outcome.value,
            reason=decision.reason.value if decision.reason else None,
            user_id=user_id,
            reference_id=reference_id,
            command_payload=command_payload,
            message=decision.message,
        )
    )
    return decision


def _limit(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{key} must be a non-negative integer")
    return value


async def _stop_or_continue(
    db: AsyncSession,
    subject: str,
    *,
    user_id: int | None = None,
    reference_id: str | None = None,
    command_payload: dict | None = None,
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
            command_payload=command_payload,
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


async def _replay_paid_start(
    command: PaidRunStartCommand, payload: dict, db: AsyncSession
) -> PaidRunStartRead | None:
    """Return the prior immutable result, if this command id was already decided."""
    existing = await db.scalar(select(Run).where(Run.id == command.id).with_for_update())
    if existing is not None:
        same_command = (
            existing.type == command.type.value
            and existing.project_id == command.project_id
            and existing.story_id == command.story_id
            and existing.task_id == command.task_id
            and existing.run_metadata == command.run_metadata
            and existing.callback_stream == command.callback_stream
        )
        if not same_command:
            raise PaidRunCommandConflict(command.id)
        return PaidRunStartRead(
            admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED), run_id=existing.id
        )
    previous_refusal = await db.scalar(
        select(WorkAdmissionAudit)
        .where(
            WorkAdmissionAudit.subject == "paid_work",
            WorkAdmissionAudit.reference_id == command.id,
        )
        .with_for_update()
    )
    if previous_refusal is None:
        return None
    if previous_refusal.command_payload != payload:
        raise PaidRunCommandConflict(command.id)
    reason = (
        WorkAdmissionReason(previous_refusal.reason)
        if previous_refusal.reason is not None
        else None
    )
    return PaidRunStartRead(
        admission=WorkAdmissionRead(
            outcome=WorkAdmissionOutcome(previous_refusal.outcome),
            reason=reason,
            retryable=reason is WorkAdmissionReason.PAID_WORK_LIMIT,
            message=previous_refusal.message,
        )
    )


async def start_paid_run(command: PaidRunStartCommand, db: AsyncSession) -> PaidRunStartRead:
    """Atomically decide and create one queued engineering or QA run.

    The config-row lock is retained through the Run INSERT and caller commit, so
    no successful decision can escape without occupying a counted slot.
    """
    payload = command.model_dump(mode="json")
    replay = await _replay_paid_start(command, payload, db)
    if replay is not None:
        return replay

    project = await db.scalar(select(Project).where(Project.id == command.project_id))
    if project is None:
        raise RuntimeError(f"Project {command.project_id} does not exist")
    user_id = project.owner_id

    # The stop row serializes starts. Recheck after taking it: another request
    # with the same id may have committed while this one waited for the lock.
    controls = await _controls(db, EMERGENCY_STOP_KEY)
    replay = await _replay_paid_start(command, payload, db)
    if replay is not None:
        return replay
    enabled = controls[EMERGENCY_STOP_KEY]
    if not isinstance(enabled, bool):
        raise RuntimeError(f"{EMERGENCY_STOP_KEY} must be a boolean")
    if enabled:
        return PaidRunStartRead(
            admission=await _audit(
                db,
                "paid_work",
                WorkAdmissionRead(
                    outcome=WorkAdmissionOutcome.DENIED,
                    reason=WorkAdmissionReason.EMERGENCY_STOP,
                ),
                user_id=user_id,
                reference_id=command.id,
                command_payload=payload,
            )
        )
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
                user_id=user_id,
                reference_id=command.id,
                command_payload=payload,
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
            admission = await _audit(
                db,
                "paid_work",
                WorkAdmissionRead(
                    outcome=WorkAdmissionOutcome.DENIED,
                    reason=WorkAdmissionReason.ENGINEERING_BUDGET_DENIED,
                ),
                user_id=user_id,
                reference_id=command.id,
                command_payload=payload,
            )
            return PaidRunStartRead(
                admission=admission,
                engineering_budget=budget,
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
        user_id=user_id,
        reference_id=command.id,
        command_payload=payload,
    )
    return PaidRunStartRead(admission=admitted, run_id=run.id)
