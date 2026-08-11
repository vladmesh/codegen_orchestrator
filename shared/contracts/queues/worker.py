from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, model_validator

from shared.contracts.base import QueueMeta
from shared.contracts.template import ServiceTemplateRef, ServiceTemplateSource
from shared.contracts.vocab import QA_EXECUTOR_AGENT_TYPES, AgentType

__all__ = [
    "AgentType",
    "QA_EXECUTOR_AGENT_TYPES",
    "WorkerCapability",
    "WorkerChannels",
    "ScaffoldConfig",
    "WorkerConfig",
    "CreateWorkerCommand",
    "DeleteWorkerCommand",
    "StatusWorkerCommand",
    "WorkerCommand",
    "CreateWorkerResponse",
    "DeleteWorkerResponse",
    "StatusWorkerResponse",
    "WorkerResponse",
]


class WorkerCapability(StrEnum):
    GIT = "git"
    GITHUB_CLI = "github_cli"
    CURL = "curl"


class WorkerChannels(StrEnum):
    """Redis stream channels and patterns."""

    # Global streams
    COMMANDS = "worker:commands"

    # Patterns
    INPUT_PATTERN = "worker:{worker_id}:input"
    OUTPUT_PATTERN = "worker:{worker_id}:output"


class ScaffoldConfig(BaseModel):
    """Configuration for scaffolding a new project via copier."""

    template_repo: ServiceTemplateSource
    template_ref: ServiceTemplateRef
    project_name: str  # sanitized name for copier
    modules: str  # "backend,tg_bot"
    task_description: str = ""


class WorkerConfig(BaseModel):
    """Worker container configuration."""

    name: str
    # "developer" writes code in a pre-scaffolded repository workspace.
    # "qa" is the central exploratory-QA executor: an ephemeral container with
    # no repository, no git credentials and nothing to commit, whose only reach
    # into a deployment is the QA runtime's typed capability endpoint.
    worker_type: Literal["developer", "qa"]
    agent_type: AgentType  # Which AI agent to use
    instructions: str  # Content for instruction file (CLAUDE.md / AGENTS.md)
    task_content: str | None = None  # Content for TASK.md (optional, for task-driven workers)
    allowed_commands: list[str]  # ["project.*", "engineering.start"]
    capabilities: list[WorkerCapability]  # ["git", "copier"]
    env_vars: dict[str, str] = {}
    auth_mode: Literal["host_session", "api_key"] = "host_session"
    host_claude_dir: str | None = None
    host_codex_home: str | None = None
    api_key: str | None = None
    project_id: str | None = None  # Project ID for workspace persistence
    repo_id: str | None = None  # Repository ID — mount pre-scaffolded workspace
    scaffold_config: ScaffoldConfig | None = None  # Scaffold phase config (copier + make setup)
    branch: str | None = None  # Story branch to checkout (e.g. "story/{story_id}")

    @model_validator(mode="after")
    def _qa_runs_on_an_assigned_subscription_agent(self) -> "WorkerConfig":
        """A `qa` worker may only be Claude Code or Codex.

        This is the second of the two places the executor is fixed — the first
        being the setting that names it — and it is the one that guards the
        wire: worker-manager validates every command against this model before
        a container exists, so a `qa` create carrying `factory` (which runs on a
        provider API key) or `noop` (which performs no testing) is refused on
        arrival instead of becoming a run that cannot do QA. Developer workers
        are unaffected and keep the full `AgentType`.
        """
        if self.worker_type == "qa" and self.agent_type not in QA_EXECUTOR_AGENT_TYPES:
            allowed = ", ".join(sorted(agent.value for agent in QA_EXECUTOR_AGENT_TYPES))
            raise ValueError(
                f"a qa worker runs on an assigned subscription agent ({allowed}), "
                f"not {self.agent_type.value}"
            )
        return self


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
