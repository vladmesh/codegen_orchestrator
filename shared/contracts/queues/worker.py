from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from shared.contracts.base import QueueMeta
from shared.contracts.template import ServiceTemplateRef, ServiceTemplateSource
from shared.contracts.vocab import QA_EXECUTOR_AGENT_TYPES, AgentType

__all__ = [
    "AgentType",
    "QA_EXECUTOR_AGENT_TYPES",
    "WorkerCapability",
    "WorkerChannels",
    "WorkerLabel",
    "WorkerOwnership",
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


class WorkerLabel(StrEnum):
    """Docker labels every dynamic worker container carries.

    They are applied by the one path that creates workers, at creation, so they
    exist before the container can exit and survive everything that happens
    after: the container's death, its removal from the API's view, and the
    deletion of its Redis metadata. A worker whose Redis record is already gone
    is still found and attributed with `docker ps -a --filter label=...`.
    """

    ID = "com.codegen.worker.id"
    TYPE = "com.codegen.type"
    PROJECT = "com.codegen.project.id"
    RUN = "com.codegen.run.id"


class WorkerOwnership(BaseModel):
    """Who a dynamic worker belongs to: one project, one run.

    Ownership is a required fact of a create request, not something observed
    afterwards. Whoever asks for a worker knows both — a developer worker is
    spawned inside an engineering run for a project, a QA executor inside a QA
    run for a project — so the answer is written down when the worker is made
    and never inferred by scanning Docker or Redis later.

    Both fields are non-empty by contract: an "unowned" worker is exactly the
    thing that cannot be attributed after it dies, so it is refused on arrival
    instead of becoming an untraceable container.
    """

    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)

    def as_labels(self) -> dict[str, str]:
        """The ownership half of a worker container's Docker labels."""
        return {
            WorkerLabel.PROJECT.value: self.project_id,
            WorkerLabel.RUN.value: self.run_id,
        }

    def as_redis_meta(self) -> dict[str, str]:
        """The same two facts, as `worker:meta:<worker_id>` fields."""
        return {"project_id": self.project_id, "run_id": self.run_id}


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
    # Who this worker belongs to. Required for every worker, developer and QA
    # alike: it is what worker-manager stamps on the container and writes to the
    # worker's Redis metadata at creation. `ownership.project_id` is also the
    # project a developer worker's workspace belongs to — there is no second
    # project field, because two of them could disagree.
    ownership: WorkerOwnership
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
