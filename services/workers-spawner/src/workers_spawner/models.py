"""Data models for worker configuration."""

from enum import Enum

from pydantic import BaseModel, Field


class AgentType(str, Enum):
    """Supported agent types."""

    CLAUDE_CODE = "claude-code"
    FACTORY_DROID = "factory-droid"
    CODEX = "codex"
    GEMINI_CLI = "gemini-cli"


class CapabilityType(str, Enum):
    """Supported capabilities that can be added to agents."""

    GIT = "git"
    CURL = "curl"
    NODE = "node"
    PYTHON = "python"
    DOCKER = "docker"


class WorkerConfig(BaseModel):
    """Declarative worker configuration.

    This config describes WHAT is needed, not HOW to set it up.
    The factories translate this into concrete Docker commands.
    """

    name: str = Field(..., description="Human-readable name for the worker")
    agent: AgentType = Field(..., description="Type of CLI agent to use")
    capabilities: list[CapabilityType] = Field(
        default_factory=list,
        description="Additional capabilities to install",
    )
    allowed_tools: list[str] = Field(
        ...,
        description="List of orchestrator-cli tool groups this agent can use",
    )
    env_vars: dict[str, str] = Field(
        default_factory=dict,
        description="Additional environment variables",
    )
    has_internet: bool = Field(
        default=True,
        description="Whether the container has internet access",
    )
    ttl_hours: int = Field(
        default=2,
        description="Container time-to-live in hours",
    )
    timeout_minutes: int = Field(
        default=10,
        description="Command execution timeout in minutes",
    )


class CreateAgentRequest(BaseModel):
    """Request to create a new agent container."""

    request_id: str = Field(..., description="Unique request identifier")
    config: WorkerConfig = Field(..., description="Worker configuration")
    context: dict[str, str] = Field(
        default_factory=dict,
        description="Additional context (user_id, project_id, etc.)",
    )


class AgentStatus(BaseModel):
    """Status of an agent container."""

    agent_id: str
    state: str  # initializing, idle, running, error, deleted
    created_at: str
    last_activity: str | None = None
    ttl_remaining_sec: int | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
