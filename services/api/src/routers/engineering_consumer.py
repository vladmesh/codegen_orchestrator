"""Audited operator control for draining engineering queue consumers."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.contracts.dto.engineering_consumer import EngineeringConsumerDrain
from shared.models import SystemConfig, WorkAdmissionAudit

from ..database import get_async_session
from ..dependencies import get_accept_result_actor

router = APIRouter(prefix="/engineering-consumer", tags=["engineering-consumer"])

DRAIN_CONFIG_KEY = "engineering.consumer_drain"


def _cleared_drain() -> EngineeringConsumerDrain:
    return EngineeringConsumerDrain(draining=False)


def _read_drain(config: SystemConfig | None) -> EngineeringConsumerDrain:
    if config is None:
        return _cleared_drain()
    return EngineeringConsumerDrain.model_validate(config.value)


async def _set_drain(*, draining: bool, actor: str, db: AsyncSession) -> EngineeringConsumerDrain:
    config = await db.get(SystemConfig, DRAIN_CONFIG_KEY, with_for_update=True)
    before = _read_drain(config)
    if draining:
        state = EngineeringConsumerDrain(
            draining=True,
            requested_at=datetime.now(UTC),
            actor=actor,
        )
    else:
        state = _cleared_drain()

    if config is None:
        config = SystemConfig(
            key=DRAIN_CONFIG_KEY,
            value=state.model_dump(mode="json"),
            category="engineering",
            description="Operator-controlled engineering consumer drain state",
            updated_by=actor,
        )
        db.add(config)
    else:
        config.value = state.model_dump(mode="json")
        config.updated_by = actor

    if before.draining != state.draining:
        db.add(
            WorkAdmissionAudit(
                subject="engineering_consumer_drain",
                outcome="draining" if draining else "resumed",
                control_name="engineering_consumer",
                before_value={"draining": before.draining},
                after_value=state.model_dump(mode="json"),
                actor=actor,
            )
        )
    await db.commit()
    return state


@router.get("/drain", response_model=EngineeringConsumerDrain)
async def get_engineering_consumer_drain(
    db: AsyncSession = Depends(get_async_session),
) -> EngineeringConsumerDrain:
    """Read the durable drain decision that every engineering consumer honors."""
    return _read_drain(await db.get(SystemConfig, DRAIN_CONFIG_KEY))


@router.post("/drain", response_model=EngineeringConsumerDrain)
async def drain_engineering_consumer(
    db: AsyncSession = Depends(get_async_session),
    actor: str = Depends(get_accept_result_actor),
) -> EngineeringConsumerDrain:
    """Stop engineering consumers from claiming further queue work."""
    return await _set_drain(draining=True, actor=actor, db=db)


@router.delete("/drain", response_model=EngineeringConsumerDrain)
async def resume_engineering_consumer(
    db: AsyncSession = Depends(get_async_session),
    actor: str = Depends(get_accept_result_actor),
) -> EngineeringConsumerDrain:
    """Clear the durable drain decision so a running consumer may claim again."""
    return await _set_drain(draining=False, actor=actor, db=db)
