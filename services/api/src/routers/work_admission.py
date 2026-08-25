"""Internal/admin surface for count-based work admission and emergency stop."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.work_admission import (
    EmergencyStopCommand,
    EmergencyStopRead,
    PaidRunStartCommand,
    PaidRunStartRead,
)
from shared.models import SystemConfig

from ..database import get_async_session
from ..dependencies import require_internal_or_admin
from ..work_admission import EMERGENCY_STOP_KEY, start_paid_run

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


@router.post("/paid-runs", response_model=PaidRunStartRead)
async def start_paid_run_endpoint(
    command: PaidRunStartCommand,
    db: AsyncSession = Depends(get_async_session),
    _: None = Depends(require_internal_or_admin),
) -> PaidRunStartRead:
    result = await start_paid_run(command, db)
    await db.commit()
    return result
