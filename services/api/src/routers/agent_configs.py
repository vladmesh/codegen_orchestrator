"""Agent configs router."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_async_session
from ..models import AgentConfig
from ..schemas.agent_config import AgentConfigCreate, AgentConfigRead, AgentConfigUpdate

router = APIRouter(prefix="/agent-configs", tags=["agent-configs"])


@router.post("/", response_model=AgentConfigRead, status_code=status.HTTP_201_CREATED)
async def create_agent_config(
    config_in: AgentConfigCreate,
    db: AsyncSession = Depends(get_async_session),
) -> AgentConfig:
    """Create a new agent configuration."""
    existing = await db.get(AgentConfig, config_in.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Agent config '{config_in.id}' already exists",
        )

    config = AgentConfig(
        id=config_in.id,
        name=config_in.name,
        system_prompt=config_in.system_prompt,
        model_name=config_in.model_name,
        temperature=config_in.temperature,
        is_active=config_in.is_active,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


@router.get("/", response_model=list[AgentConfigRead])
async def list_agent_configs(
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[AgentConfig]:
    """List all agent configurations."""
    query = select(AgentConfig)
    if is_active is not None:
        query = query.where(AgentConfig.is_active == is_active)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{config_id}", response_model=AgentConfigRead)
async def get_agent_config(
    config_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> AgentConfig:
    """Get a specific agent configuration."""
    config = await db.get(AgentConfig, config_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent config '{config_id}' not found",
        )
    return config


@router.patch("/{config_id}", response_model=AgentConfigRead)
async def update_agent_config(
    config_id: str,
    updates: AgentConfigUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> AgentConfig:
    """Update an agent configuration."""
    config = await db.get(AgentConfig, config_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent config '{config_id}' not found",
        )

    # Apply updates
    update_data = updates.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    # Increment version on any update
    if update_data:
        config.version += 1

    await db.commit()
    await db.refresh(config)
    return config


@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_config(
    config_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete an agent configuration."""
    config = await db.get(AgentConfig, config_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent config '{config_id}' not found",
        )

    await db.delete(config)
    await db.commit()
