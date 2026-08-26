"""Internal/admin surface for count-based work admission and emergency stop."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.work_admission import (
    EmergencyStopCommand,
    EmergencyStopRead,
    PaidRunStartCommand,
    PaidRunStartRead,
    WorkAdmissionControlCommand,
)
from shared.models import SystemConfig

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..work_admission import (
    EMERGENCY_STOP_KEY,
    MAX_PAID_RUNS_KEY,
    MAX_PROJECTS_KEY,
    PaidRunCommandConflict,
    start_paid_run,
)

router = APIRouter(prefix="/work-admission", tags=["work-admission"])


async def _stop_config(db: AsyncSession) -> SystemConfig:
    config = await db.scalar(
        select(SystemConfig).where(SystemConfig.key == EMERGENCY_STOP_KEY).with_for_update()
    )
    if config is None:
        raise RuntimeError("Missing work admission emergency-stop config")
    return config


@router.get("/emergency-stop", response_model=EmergencyStopRead)
async def get_emergency_stop(
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EmergencyStopRead:
    config = await _stop_config(db)
    if not isinstance(config.value, bool):
        raise RuntimeError(f"{EMERGENCY_STOP_KEY} must be a boolean")
    return EmergencyStopRead(enabled=config.value)


@router.put("/emergency-stop", response_model=EmergencyStopRead)
async def put_emergency_stop(
    command: EmergencyStopCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> EmergencyStopRead:
    config = await _stop_config(db)
    config.value = command.enabled
    await db.commit()
    return EmergencyStopRead(enabled=command.enabled)


@router.put("/controls/{key:path}")
async def put_work_admission_control(
    key: str,
    command: WorkAdmissionControlCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> dict[str, object]:
    """The only mutation path for protected admission controls."""
    if key == EMERGENCY_STOP_KEY:
        if not isinstance(command.value, bool):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
    elif key in {MAX_PROJECTS_KEY, MAX_PAID_RUNS_KEY}:
        if (
            not isinstance(command.value, int)
            or isinstance(command.value, bool)
            or command.value < 0
        ):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    config = await db.get(SystemConfig, key)
    if config is None:
        raise RuntimeError(f"Missing work admission config: {key}")
    config.value = command.value
    await db.commit()
    return {"key": key, "value": command.value}


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
    await db.commit()
    return result
