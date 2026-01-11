from enum import Enum
from typing import Literal

from pydantic import BaseModel

from shared.contracts.base import QueueMeta


class AgentType(str, Enum):
    CLAUDE = "claude"  # Claude Code
    FACTORY = "factory"  # Factory.ai Droid


class WorkerCapability(str, Enum):
    GIT = "git"
    GITHUB_CLI = "github_cli"
    # Copier moved to dedicated service
    CURL = "curl"
    DOCKER = "docker"  # dind mount


class WorkerConfig(BaseModel):
    """Worker container configuration."""

    name: str
    worker_type: Literal["po", "developer"]  # Worker type for queue naming
    agent_type: AgentType  # Which AI agent to use
    instructions: str  # Content for CLAUDE.md / AGENTS.md
    allowed_commands: list[str]  # ["project.*", "engineering.start"]
    capabilities: list[WorkerCapability]  # ["git", "copier"]
    env_vars: dict[str, str] = {}


class CreateWorkerCommand(QueueMeta):
    """Create new worker."""

    command: Literal["create"] = "create"
    request_id: str
    config: WorkerConfig
    context: dict[str, str] = {}  # Additional context (user_id, task_id, etc.)


class DeleteWorkerCommand(QueueMeta):
    """Delete worker."""

    command: Literal["delete"] = "delete"
    request_id: str
    worker_id: str


class StatusWorkerCommand(QueueMeta):
    """Get worker status."""

    command: Literal["status"] = "status"
    request_id: str
    worker_id: str


WorkerCommand = CreateWorkerCommand | DeleteWorkerCommand | StatusWorkerCommand


class CreateWorkerResponse(BaseModel):
    """Response to create command."""

    request_id: str
    success: bool
    worker_id: str | None = None
    error: str | None = None


class DeleteWorkerResponse(BaseModel):
    """Response to delete command."""

    request_id: str
    success: bool
    error: str | None = None


class StatusWorkerResponse(BaseModel):
    """Response to status command."""

    request_id: str
    success: bool
    status: Literal["starting", "running", "stopped", "failed"] | None = None
    error: str | None = None


WorkerResponse = CreateWorkerResponse | DeleteWorkerResponse | StatusWorkerResponse
