"""Atomic count-based admission used by every paid-work entry point."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_budget_policy import EngineeringBudgetReservationState
from shared.contracts.dto.executor_decision import ExecutorDecision, ExecutorOverride
from shared.contracts.dto.executor_diagnostics import (
    ExecutorAvailability,
    ExecutorDiagnostic,
    ExecutorDiagnosticSnapshot,
)
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.work_admission import (
    PaidRunStartCommand,
    PaidRunStartRead,
    WorkAdmissionOutcome,
    WorkAdmissionRead,
    WorkAdmissionReason,
)
from shared.models import (
    EngineeringBudgetReservation,
    Project,
    Run,
    SystemConfig,
    User,
    WorkAdmissionAudit,
)

from .config import get_settings
from .executor_diagnostics import current_executor_diagnostic
from .executor_resolver import resolve_executor_decision

EMERGENCY_STOP_KEY = "work_admission.emergency_stop"
MAX_PROJECTS_KEY = "work_admission.max_projects_per_user"
MAX_PAID_RUNS_KEY = "work_admission.max_concurrent_paid_runs"
ENGINEERING_EXECUTOR_OVERRIDE_KEY = "work_admission.engineering_executor_override"
QA_EXECUTOR_OVERRIDE_KEY = "work_admission.qa_executor_override"
PAID_WORK_CONTROL_KEYS = tuple(
    sorted(
        (
            EMERGENCY_STOP_KEY,
            MAX_PAID_RUNS_KEY,
            ENGINEERING_EXECUTOR_OVERRIDE_KEY,
            QA_EXECUTOR_OVERRIDE_KEY,
        )
    )
)

_REFUSAL_MESSAGES = {
    WorkAdmissionReason.EMERGENCY_STOP: "Запуск новой работы временно остановлен оператором.",
    WorkAdmissionReason.PROJECT_LIMIT: "Достигнут лимит активных проектов. Попробуйте позже.",
    WorkAdmissionReason.PAID_WORK_LIMIT: (
        "Достигнут лимит одновременной платной работы. Попробуйте позже."
    ),
    WorkAdmissionReason.ENGINEERING_BUDGET_DENIED: (
        "Недостаточно доступного бюджета для запуска инженерной задачи."
    ),
    WorkAdmissionReason.EXECUTOR_UNAVAILABLE: "Выбранный исполнитель сейчас недоступен.",
    WorkAdmissionReason.EXECUTOR_CONFIRMATION_REQUIRED: (
        "Текущее состояние исполнителя неизвестно и требует подтверждения администратора."
    ),
}


class PaidRunCommandConflict(Exception):
    """A stable paid-run id was replayed with different immutable input."""


class PaidRunIdentityExpired(Exception):
    """A terminal attempt id cannot be reused for a new paid attempt."""


async def _controls(db: AsyncSession, *keys: str) -> dict[str, object]:
    rows = (
        await db.scalars(
            select(SystemConfig)
            .where(SystemConfig.key.in_(keys))
            .order_by(SystemConfig.key)
            .with_for_update()
        )
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


def _override(value: object, key: str) -> ExecutorOverride:
    if not isinstance(value, str):
        raise RuntimeError(f"{key} must be none, claude, or codex")
    try:
        return ExecutorOverride(value)
    except ValueError as exc:
        raise RuntimeError(f"{key} must be none, claude, or codex") from exc


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
    """Return only a still-live start with its still-active engineering hold.

    Audit records retain command identity for conflicts but never cache a
    refusal: controls must decide every retry anew.
    """
    existing = await db.scalar(select(Run).where(Run.id == command.id).with_for_update())
    prior_payloads = (
        await db.scalars(
            select(WorkAdmissionAudit.command_payload).where(
                WorkAdmissionAudit.subject == "paid_work",
                WorkAdmissionAudit.reference_id == command.id,
            )
        )
    ).all()
    # The audit is the immutable command record.  A Run's metadata is deliberately
    # mutable: dispatchers add delivery stamps and workers add operational facts.
    # Comparing it here would turn engine bookkeeping into a caller conflict.
    if any(prior_payload != payload for prior_payload in prior_payloads):
        raise PaidRunCommandConflict(command.id)
    if existing is not None and not prior_payloads:
        # Runs created outside this command have no immutable command identity and
        # must not become replays merely because their current fields happen to fit.
        raise PaidRunCommandConflict(command.id)
    if existing is None:
        return None
    if existing.status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
        raise PaidRunIdentityExpired(command.id)
    if command.type is RunType.ENGINEERING:
        reservation = await db.scalar(
            select(EngineeringBudgetReservation)
            .where(EngineeringBudgetReservation.attempt_id == command.id)
            .with_for_update()
        )
        if (
            reservation is not None
            and reservation.state is not EngineeringBudgetReservationState.ACTIVE
        ):
            return None
    return PaidRunStartRead(
        admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED),
        run_id=existing.id,
        executor_decision=ExecutorDecision.from_run_metadata(existing.run_metadata),
    )


async def _has_current_unknown_confirmation(
    executor: ExecutorDecision, snapshot: ExecutorDiagnosticSnapshot | None, db: AsyncSession
) -> bool:
    """A confirmation is reusable for any start until this exact snapshot expires."""
    if snapshot is None or executor.agent_type.value not in {"claude", "codex"}:
        return False
    rows = (
        await db.scalars(
            select(WorkAdmissionAudit).where(
                WorkAdmissionAudit.subject == "executor_diagnostic_confirmation",
                WorkAdmissionAudit.reference_id == snapshot.version,
            )
        )
    ).all()
    now = datetime.now(UTC)
    for row in rows:
        payload = row.command_payload or {}
        expiry = (
            (row.after_value or {}).get("expires_at") if isinstance(row.after_value, dict) else None
        )
        if payload.get("executor") != executor.agent_type.value or not isinstance(expiry, str):
            continue
        try:
            if datetime.fromisoformat(expiry) > now:
                return True
        except ValueError:
            continue
    return False


async def start_paid_run(command: PaidRunStartCommand, db: AsyncSession) -> PaidRunStartRead:
    """Atomically decide and create one queued engineering or QA run.

    The config-row lock is retained through the Run INSERT and caller commit, so
    no successful decision can escape without occupying a counted slot.
    """
    payload = command.model_dump(mode="json")
    replay = await _replay_paid_start(command, payload, db)
    if replay is not None:
        return replay

    # The complete paid-control set serializes starts. Recheck after taking it:
    # another request with the same id may have committed while this one waited.
    # Resolve only after this replay so concurrent starts cannot make a discarded
    # decision before the Run that owns the first decision is committed.
    controls = await _controls(db, *PAID_WORK_CONTROL_KEYS)
    replay = await _replay_paid_start(command, payload, db)
    if replay is not None:
        return replay
    project = await db.scalar(select(Project).where(Project.id == command.project_id))
    if project is None:
        raise RuntimeError(f"Project {command.project_id} does not exist")
    user_id = project.owner_id
    override_key = (
        ENGINEERING_EXECUTOR_OVERRIDE_KEY
        if command.type is RunType.ENGINEERING
        else QA_EXECUTOR_OVERRIDE_KEY
    )
    decision = resolve_executor_decision(
        command.type,
        project.config,
        get_settings(),
        global_override=_override(controls[override_key], override_key),
    )
    diagnostic: ExecutorDiagnostic | None = None
    if decision.agent_type.value in {"claude", "codex"}:
        diagnostic, snapshot = await current_executor_diagnostic(decision.agent_type)
        if diagnostic.availability is ExecutorAvailability.UNAVAILABLE:
            return PaidRunStartRead(
                admission=await _audit(
                    db,
                    "paid_work",
                    WorkAdmissionRead(
                        outcome=WorkAdmissionOutcome.DENIED,
                        reason=WorkAdmissionReason.EXECUTOR_UNAVAILABLE,
                    ),
                    user_id=user_id,
                    reference_id=command.id,
                    command_payload=payload,
                ),
                executor_decision=decision,
                executor_diagnostic=diagnostic,
            )
        if (
            diagnostic.availability is ExecutorAvailability.UNKNOWN
            and not await _has_current_unknown_confirmation(decision, snapshot, db)
        ):
            return PaidRunStartRead(
                admission=await _audit(
                    db,
                    "paid_work",
                    WorkAdmissionRead(
                        outcome=WorkAdmissionOutcome.DEFERRED,
                        reason=WorkAdmissionReason.EXECUTOR_CONFIRMATION_REQUIRED,
                    ),
                    user_id=user_id,
                    reference_id=command.id,
                    command_payload=payload,
                ),
                executor_decision=decision,
                executor_diagnostic=diagnostic,
            )
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
    restarting = await db.scalar(select(Run).where(Run.id == command.id).with_for_update())
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(Run)
            .where(
                Run.type.in_((RunType.ENGINEERING.value, RunType.QA.value)),
                Run.status.in_((RunStatus.QUEUED.value, RunStatus.RUNNING.value)),
                Run.id != command.id if restarting is not None else True,
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

    if restarting is None:
        run = Run(
            id=command.id,
            type=command.type.value,
            status=RunStatus.QUEUED.value,
            project_id=command.project_id,
            story_id=command.story_id,
            task_id=command.task_id,
            run_metadata={**command.run_metadata, **decision.as_run_metadata()},
            callback_stream=command.callback_stream,
        )
        db.add(run)
    else:
        if restarting.status not in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
            raise PaidRunIdentityExpired(command.id)
        run = restarting
        run.status = RunStatus.QUEUED.value
        run.result = None
        run.error_message = None
        run.error_traceback = None
        run.started_at = None
        run.completed_at = None
    admitted = await _audit(
        db,
        "paid_work",
        WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED),
        user_id=user_id,
        reference_id=command.id,
        command_payload=payload,
    )
    return PaidRunStartRead(
        admission=admitted,
        run_id=run.id,
        executor_decision=decision,
        executor_diagnostic=diagnostic,
    )


async def abort_paid_run_pre_handoff(run_id: str, reason: str, db: AsyncSession) -> None:
    """Atomically close an unpublished paid run and release its engineering hold."""
    run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run is None:
        return
    if run.status in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}:
        # CANCELLED is the only terminal status that deliberately carries no
        # worker result.  This state means the message was proven not to have
        # reached a queue; FAILED would claim a worker outcome that does not exist.
        run.status = RunStatus.CANCELLED.value
        run.error_message = reason
        run.run_metadata = {**(run.run_metadata or {}), "pre_handoff_aborted": True}
    if run.type == RunType.ENGINEERING.value:
        from .engineering_budget_admission import release_pre_handoff_reservation

        await release_pre_handoff_reservation(run_id, db)
