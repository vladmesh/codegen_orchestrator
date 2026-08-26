"""Internal/admin commands for paid-work admission and its operator controls."""

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.work_admission import (
    EmergencyStopCommand,
    EmergencyStopRead,
    PaidRunStartCommand,
    PaidRunStartRead,
    PaidWorkControlsCommand,
    PaidWorkControlsRead,
    WorkAdmissionControlCommand,
)
from shared.models import SystemConfig, WorkAdmissionAudit

from ..database import get_async_session
from ..dependencies import get_internal_or_admin_actor, require_internal_or_admin
from ..work_admission import (
    EMERGENCY_STOP_KEY,
    ENGINEERING_EXECUTOR_OVERRIDE_KEY,
    MAX_PAID_RUNS_KEY,
    MAX_PROJECTS_KEY,
    PAID_WORK_CONTROL_KEYS,
    QA_EXECUTOR_OVERRIDE_KEY,
    PaidRunCommandConflict,
    PaidRunIdentityExpired,
    _controls,
    _limit,
    _override,
    abort_paid_run_pre_handoff,
    start_paid_run,
)

router = APIRouter(prefix="/work-admission", tags=["work-admission"])

_CONTROL_FIELDS = {
    EMERGENCY_STOP_KEY: "emergency_stop",
    MAX_PAID_RUNS_KEY: "max_concurrent_paid_runs",
    ENGINEERING_EXECUTOR_OVERRIDE_KEY: "engineering_executor_override",
    QA_EXECUTOR_OVERRIDE_KEY: "qa_executor_override",
}
_CONTROL_DESCRIPTIONS = {
    EMERGENCY_STOP_KEY: "Emergency stop for new projects, engineering and QA work",
    MAX_PROJECTS_KEY: "Maximum number of projects per user",
    MAX_PAID_RUNS_KEY: "Maximum number of concurrent engineering and QA runs",
    ENGINEERING_EXECUTOR_OVERRIDE_KEY: "Global executor override for engineering attempts",
    QA_EXECUTOR_OVERRIDE_KEY: "Global executor override for QA attempts",
}


def _paid_work_controls(values: dict[str, object]) -> PaidWorkControlsRead:
    enabled = values[EMERGENCY_STOP_KEY]
    if not isinstance(enabled, bool):
        raise RuntimeError(f"{EMERGENCY_STOP_KEY} must be a boolean")
    return PaidWorkControlsRead(
        emergency_stop=enabled,
        max_concurrent_paid_runs=_limit(values[MAX_PAID_RUNS_KEY], MAX_PAID_RUNS_KEY),
        engineering_executor_override=_override(
            values[ENGINEERING_EXECUTOR_OVERRIDE_KEY], ENGINEERING_EXECUTOR_OVERRIDE_KEY
        ),
        qa_executor_override=_override(values[QA_EXECUTOR_OVERRIDE_KEY], QA_EXECUTOR_OVERRIDE_KEY),
    )


async def _replace_paid_work_controls(
    command: PaidWorkControlsCommand, actor: str, db: AsyncSession
) -> PaidWorkControlsRead:
    """Lock all paid controls in key order, mutate, and append one fact per change."""
    requested = command.model_dump(mode="json")
    rows = (
        await db.scalars(
            select(SystemConfig)
            .where(SystemConfig.key.in_(PAID_WORK_CONTROL_KEYS))
            .order_by(SystemConfig.key)
            .with_for_update()
        )
    ).all()
    values = {row.key: row.value for row in rows}
    for key, field in _CONTROL_FIELDS.items():
        if key not in values:
            value = requested[field]
            db.add(
                SystemConfig(
                    key=key,
                    value=value,
                    category="work_admission",
                    description=_CONTROL_DESCRIPTIONS[key],
                    updated_by="work_admission_control",
                )
            )
            values[key] = value
    _paid_work_controls(values)
    for key, field in _CONTROL_FIELDS.items():
        before = values[key]
        after = requested[field]
        if before == after:
            continue
        row = next((row for row in rows if row.key == key), None)
        if row is None:
            # Newly initialized values above already equal the requested state.
            continue
        row.value = after
        db.add(
            WorkAdmissionAudit(
                subject="paid_work_control",
                outcome="changed",
                control_name=field,
                before_value=before,
                after_value=after,
                actor=actor,
            )
        )
    return PaidWorkControlsRead.model_validate(requested)


@router.get("/controls", response_model=PaidWorkControlsRead)
async def get_paid_work_controls(
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> PaidWorkControlsRead:
    return _paid_work_controls(await _controls(db, *PAID_WORK_CONTROL_KEYS))


@router.put("/controls", response_model=PaidWorkControlsRead)
async def put_paid_work_controls(
    command: PaidWorkControlsCommand,
    db: AsyncSession = Depends(get_async_session),
    actor: str = Depends(get_internal_or_admin_actor),
) -> PaidWorkControlsRead:
    controls = await _replace_paid_work_controls(command, actor, db)
    await db.commit()
    return controls


@router.get("/emergency-stop", response_model=EmergencyStopRead)
async def get_emergency_stop(
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EmergencyStopRead:
    return EmergencyStopRead(enabled=(await get_paid_work_controls(db=db)).emergency_stop)


@router.put("/emergency-stop", response_model=EmergencyStopRead)
async def put_emergency_stop(
    command: EmergencyStopCommand,
    db: AsyncSession = Depends(get_async_session),
    actor: str = Depends(get_internal_or_admin_actor),
) -> EmergencyStopRead:
    current = _paid_work_controls(await _controls(db, *PAID_WORK_CONTROL_KEYS))
    updated = await _replace_paid_work_controls(
        PaidWorkControlsCommand(**{**current.model_dump(), "emergency_stop": command.enabled}),
        actor,
        db,
    )
    await db.commit()
    return EmergencyStopRead(enabled=updated.emergency_stop)


@router.put("/controls/{key:path}")
async def put_work_admission_control(
    key: str,
    command: WorkAdmissionControlCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> dict[str, object]:
    """Legacy typed mutation retained only for the non-paid project ceiling."""
    if key != MAX_PROJECTS_KEY:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not isinstance(command.value, int) or isinstance(command.value, bool) or command.value < 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
    row = await db.scalar(select(SystemConfig).where(SystemConfig.key == key).with_for_update())
    if row is None:
        row = SystemConfig(
            key=key,
            value=command.value,
            category="work_admission",
            description=_CONTROL_DESCRIPTIONS[key],
            updated_by="work_admission_control",
        )
        db.add(row)
        await db.commit()
        return {"key": key, "value": command.value, "previous": None}
    previous = row.value
    row.value = command.value
    await db.commit()
    return {"key": key, "value": command.value, "previous": previous}


@router.post("/paid-runs", response_model=PaidRunStartRead)
async def start_paid_run_endpoint(
    command: PaidRunStartCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> PaidRunStartRead:
    try:
        result = await start_paid_run(command, db)
    except PaidRunCommandConflict as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "paid_run_command_conflict", "id": str(exc)},
        ) from exc
    except PaidRunIdentityExpired as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "paid_run_identity_expired", "id": str(exc)},
        ) from exc
    await db.commit()
    return result


@router.post("/paid-runs/{run_id}/abort-pre-handoff", status_code=status.HTTP_204_NO_CONTENT)
async def abort_paid_run_pre_handoff_endpoint(
    run_id: str,
    reason: str = Body(embed=True),
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> None:
    await abort_paid_run_pre_handoff(run_id, reason, db)
    await db.commit()
