from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

from shared.contracts.base import QueueMeta


class AgentType(StrEnum):
    CLAUDE = "claude"  # Claude Code
    FACTORY = "factory"  # Factory.ai Droid


class WorkerCapability(StrEnum):
    GIT = "git"
    GITHUB_CLI = "github_cli"
    CURL = "curl"


class WorkerChannels(StrEnum):
    """Redis stream channels and patterns."""

    # Global streams
    COMMANDS = "worker:commands"
    LIFECYCLE = "worker:lifecycle"

    # Patterns
    INPUT_PATTERN = "worker:{worker_id}:input"
    OUTPUT_PATTERN = "worker:{worker_id}:output"


class ScaffoldConfig(BaseModel):
    """Configuration for scaffolding a new project via copier."""

    template_repo: str  # "gh:project-factory-organization/service-template"
    project_name: str  # sanitized name for copier
    modules: str  # "backend,tg_bot"
    task_description: str = ""


class WorkerConfig(BaseModel):
    """Worker container configuration."""

    name: str
    worker_type: Literal["developer"]  # Worker type for queue naming
    agent_type: AgentType  # Which AI agent to use
    instructions: str  # Content for instruction file (CLAUDE.md / AGENTS.md)
    task_content: str | None = None  # Content for TASK.md (optional, for task-driven workers)
    allowed_commands: list[str]  # ["project.*", "engineering.start"]
    capabilities: list[WorkerCapability]  # ["git", "copier"]
    env_vars: dict[str, str] = {}
    auth_mode: Literal["host_session", "api_key"] = "host_session"
    host_claude_dir: str | None = None
    api_key: str | None = None
    project_id: str | None = None  # Project ID for workspace persistence
    scaffold_config: ScaffoldConfig | None = None  # Scaffold phase config (copier + make setup)


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
    reason: Literal["completed", "failed", "timeout"] | None = None


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
