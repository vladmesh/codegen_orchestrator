"""System configs router — CRUD for operational constants."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import SystemConfig

from ..database import get_async_session
from ..schemas.system_config import SystemConfigCreate, SystemConfigRead, SystemConfigUpdate

router = APIRouter(prefix="/system-configs", tags=["system-configs"])


@router.post("/", response_model=SystemConfigRead, status_code=status.HTTP_201_CREATED)
async def create_or_update_system_config(
    data: SystemConfigCreate,
    db: AsyncSession = Depends(get_async_session),
) -> SystemConfig:
    """Create a system config, or update if key already exists (upsert)."""
    existing = await db.get(SystemConfig, data.key)
    if existing:
        for field, val in data.model_dump(exclude={"key"}).items():
            if val is not None:
                setattr(existing, field, val)
        await db.commit()
        await db.refresh(existing)
        return existing

    config = SystemConfig(**data.model_dump())
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


@router.get("/", response_model=list[SystemConfigRead])
async def list_system_configs(
    category: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[SystemConfig]:
    """List system configs, optionally filtered by category."""
    query = select(SystemConfig).order_by(SystemConfig.key)
    if category:
        query = query.where(SystemConfig.category == category)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{key:path}", response_model=SystemConfigRead)
async def get_system_config(
    key: str,
    db: AsyncSession = Depends(get_async_session),
) -> SystemConfig:
    """Get a specific system config by key."""
    config = await db.get(SystemConfig, key)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System config '{key}' not found",
        )
    return config


@router.patch("/{key:path}", response_model=SystemConfigRead)
async def update_system_config(
    key: str,
    updates: SystemConfigUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> SystemConfig:
    """Update a system config."""
    config = await db.get(SystemConfig, key)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System config '{key}' not found",
        )

    update_data = updates.model_dump(exclude_unset=True)
    for field, val in update_data.items():
        setattr(config, field, val)

    await db.commit()
    await db.refresh(config)
    return config


@router.delete("/{key:path}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_system_config(
    key: str,
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete a system config."""
    config = await db.get(SystemConfig, key)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"System config '{key}' not found",
        )
    await db.delete(config)
    await db.commit()
