"""Pydantic schemas for CLI agent configuration."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CLIAgentConfigBase(BaseModel):
    """Base schema for CLI agent config."""

    name: str = Field(..., description="Display name for the agent")
    provider: str = Field(..., description="Provider type (factory, claude, codex)")
    model_name: str | None = Field(None, description="Model to use (if applicable)")
    prompt_template: str = Field(..., description="Task prompt template")
    timeout_seconds: int = Field(600, description="Execution timeout in seconds")
    workspace_image: str | None = Field(None, description="Docker image for workspace")
    required_credentials: list[str] = Field(
        default_factory=list, description="List of secret keys needed"
    )
    provider_settings: dict[str, Any] = Field(
        default_factory=dict, description="Provider-specific settings"
    )
    is_active: bool = Field(True, description="Whether the agent is active")


class CLIAgentConfigCreate(CLIAgentConfigBase):
    """Schema for creating a CLI agent config."""

    id: str = Field(..., description="Unique identifier (e.g. architect.spawn_worker)")


class CLIAgentConfigUpdate(BaseModel):
    """Schema for updating a CLI agent config."""

    name: str | None = None
    provider: str | None = None
    model_name: str | None = None
    prompt_template: str | None = None
    timeout_seconds: int | None = None
    workspace_image: str | None = None
    required_credentials: list[str] | None = None
    provider_settings: dict[str, Any] | None = None
    is_active: bool | None = None


class CLIAgentConfigRead(CLIAgentConfigBase):
    """Schema for reading a CLI agent config."""

    id: str
    version: int

    model_config = ConfigDict(from_attributes=True)
