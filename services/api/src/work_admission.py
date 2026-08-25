"""Atomic count-based admission used by every paid-work entry point."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.server import ServerStatus
from shared.contracts.dto.work_admission import (
    WorkAdmissionOutcome,
    WorkAdmissionRead,
    WorkAdmissionReason,
)
from shared.models import Project, Run, Server, SystemConfig, User, WorkAdmissionAudit

EMERGENCY_STOP_KEY = "work_admission.emergency_stop"
MAX_PROJECTS_KEY = "work_admission.max_projects_per_user"
MAX_MANAGED_SERVERS_KEY = "work_admission.max_active_managed_servers"
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
    if controls[EMERGENCY_STOP_KEY] is True:
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


async def admit_server_provisioning(handle: str, db: AsyncSession) -> WorkAdmissionRead:
    """Serialize provisioning starts against the active managed-server ceiling."""
    stopped = await _stop_or_continue(db, "provisioning", reference_id=handle)
    if stopped is not None:
        return stopped
    controls = await _controls(db, MAX_MANAGED_SERVERS_KEY)
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(Server)
            .where(
                Server.is_managed.is_(True),
                Server.status.not_in(
                    (ServerStatus.MISSING.value, ServerStatus.DECOMMISSIONED.value)
                ),
            )
        )
        or 0
    )
    if count > _limit(controls[MAX_MANAGED_SERVERS_KEY], MAX_MANAGED_SERVERS_KEY):
        return await _audit(
            db,
            "provisioning",
            WorkAdmissionRead(
                outcome=WorkAdmissionOutcome.DENIED,
                reason=WorkAdmissionReason.MANAGED_SERVER_LIMIT,
            ),
            reference_id=handle,
        )
    return await _audit(
        db,
        "provisioning",
        WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED),
        reference_id=handle,
    )


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
