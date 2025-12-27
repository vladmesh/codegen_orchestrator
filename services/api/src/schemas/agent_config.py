"""Pydantic schemas for agent configuration."""

from pydantic import BaseModel, ConfigDict, Field


class AgentConfigBase(BaseModel):
    """Base schema with common fields."""

    name: str = Field(..., description="Display name for the agent")
    system_prompt: str = Field(..., description="System prompt for the agent")
    model_name: str = Field(default="gpt-4o", description="LLM model name")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0, description="LLM temperature")
    is_active: bool = Field(default=True, description="Whether agent is active")

    # OpenRouter integration fields (Phase 1)
    llm_provider: str = Field(
        default="openrouter", description="LLM provider (openrouter, openai, anthropic)"
    )
    model_identifier: str = Field(
        default="openai/gpt-4o", description="Full model identifier (e.g., openai/gpt-4o)"
    )
    openrouter_site_url: str | None = Field(
        default=None, description="Site URL for OpenRouter analytics"
    )
    openrouter_app_name: str | None = Field(
        default=None, description="App name for OpenRouter analytics"
    )


class AgentConfigCreate(AgentConfigBase):
    """Schema for creating a new agent config."""

    id: str = Field(..., min_length=1, max_length=50, description="Agent identifier")


class AgentConfigRead(AgentConfigBase):
    """Schema for reading agent config."""

    id: str
    version: int

    model_config = ConfigDict(from_attributes=True)


class AgentConfigUpdate(BaseModel):
    """Schema for updating agent config (all fields optional)."""

    name: str | None = None
    system_prompt: str | None = None
    model_name: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    is_active: bool | None = None

    # OpenRouter integration fields
    llm_provider: str | None = None
    model_identifier: str | None = None
    openrouter_site_url: str | None = None
    openrouter_app_name: str | None = None
