"""CLI Agent configs router."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models import CLIAgentConfig

from ..database import get_async_session
from ..schemas.cli_agent_config import (
    CLIAgentConfigCreate,
    CLIAgentConfigRead,
    CLIAgentConfigUpdate,
)

router = APIRouter(prefix="/cli-agent-configs", tags=["cli-agent-configs"])


@router.post("/", response_model=CLIAgentConfigRead, status_code=status.HTTP_201_CREATED)
async def create_cli_agent_config(
    config_in: CLIAgentConfigCreate,
    db: AsyncSession = Depends(get_async_session),
) -> CLIAgentConfig:
    """Create a new CLI agent configuration."""
    existing = await db.get(CLIAgentConfig, config_in.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"CLI Agent config '{config_in.id}' already exists",
        )

    config = CLIAgentConfig(
        id=config_in.id,
        name=config_in.name,
        provider=config_in.provider,
        model_name=config_in.model_name,
        prompt_template=config_in.prompt_template,
        timeout_seconds=config_in.timeout_seconds,
        workspace_image=config_in.workspace_image,
        required_credentials=config_in.required_credentials,
        provider_settings=config_in.provider_settings,
        is_active=config_in.is_active,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


@router.get("/", response_model=list[CLIAgentConfigRead])
async def list_cli_agent_configs(
    is_active: bool | None = None,
    provider: str | None = None,
    db: AsyncSession = Depends(get_async_session),
) -> list[CLIAgentConfig]:
    """List all CLI agent configurations."""
    query = select(CLIAgentConfig)
    if is_active is not None:
        query = query.where(CLIAgentConfig.is_active == is_active)
    if provider is not None:
        query = query.where(CLIAgentConfig.provider == provider)

    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{config_id}", response_model=CLIAgentConfigRead)
async def get_cli_agent_config(
    config_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> CLIAgentConfig:
    """Get a specific CLI agent configuration."""
    config = await db.get(CLIAgentConfig, config_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CLI Agent config '{config_id}' not found",
        )
    return config


@router.patch("/{config_id}", response_model=CLIAgentConfigRead)
async def update_cli_agent_config(
    config_id: str,
    updates: CLIAgentConfigUpdate,
    db: AsyncSession = Depends(get_async_session),
) -> CLIAgentConfig:
    """Update a CLI agent configuration."""
    config = await db.get(CLIAgentConfig, config_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CLI Agent config '{config_id}' not found",
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
async def delete_cli_agent_config(
    config_id: str,
    db: AsyncSession = Depends(get_async_session),
) -> None:
    """Delete a CLI agent configuration."""
    config = await db.get(CLIAgentConfig, config_id)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"CLI Agent config '{config_id}' not found",
        )

    await db.delete(config)
    await db.commit()
