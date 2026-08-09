import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

from shared.contracts.vocab import AgentType


class WorkerWrapperConfig(BaseSettings):
    """Configuration for Worker Wrapper."""

    broker_url: str = Field(min_length=1, description="Authenticated worker-broker URL")
    broker_token: str = Field(min_length=32, description="Per-worker broker credential")
    worker_id: str = Field(
        validation_alias="WORKER_ID", min_length=1, description="Worker identity"
    )
    agent_type: AgentType = Field(..., description="Which coding agent runs in this worker")
    auth_mode: str = Field(
        default="host_session",
        description="How the agent authenticates: host_session (mounted session) or api_key",
    )

    # Optional execution settings
    poll_interval_ms: int = 500
    # Fallback when WORKER_SUBPROCESS_TIMEOUT_SECONDS is unset; worker-manager
    # normally forwards its own value. Sized for live LLM agents, not noop.
    subprocess_timeout_seconds: int = 900
    http_server_port: int = 9090
    transcript_dir: str = "/artifacts/worker-transcripts"
    transcript_max_bytes: int = 5 * 1024 * 1024

    # Not WORKER_-prefixed: this is the Claude CLI's own variable, set by
    # worker-manager to the mounted host session directory.
    claude_config_dir: str | None = Field(
        default=None,
        validation_alias="CLAUDE_CONFIG_DIR",
        description="Directory the Claude CLI keeps .claude.json and its session in",
    )

    model_config = {"env_prefix": "WORKER_", "populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def reject_direct_control_plane_transport(cls, values):
        forbidden = {
            "WORKER_REDIS_URL",
            "WORKER_API_URL",
            "WORKER_MANAGER_URL",
            "SECRETS_ENCRYPTION_KEY",
            "redis_url",
            "api_url",
            "worker_manager_url",
        }
        if isinstance(values, dict):
            supplied = forbidden.intersection(values)
            if supplied:
                raise ValueError(
                    f"direct worker transport is forbidden: {', '.join(sorted(supplied))}"
                )
        return values


def validate_agent_config(config: WorkerWrapperConfig) -> None:
    """Reject a worker whose agent cannot keep its state, at container start.

    A Claude worker in host_session mode keeps its whole CLI state — .claude.json,
    its backups and the session — in CLAUDE_CONFIG_DIR, which must be the host
    directory bind-mounted into the container. Anything else (the directory baked
    into the image, a path Docker created on the fly, a read-only mount) means the
    CLI starts from empty state and loses it again on restart. That has to surface
    here, naming what is missing, not as an agent failure mid-round.

    api_key mode authenticates per call and keeps no session, so it needs none of this.
    """
    forbidden = (
        "WORKER_REDIS_URL",
        "WORKER_API_URL",
        "WORKER_MANAGER_URL",
        "SECRETS_ENCRYPTION_KEY",
    )
    supplied = [name for name in forbidden if os.environ.get(name)]
    if supplied:
        raise RuntimeError(f"direct worker transport is forbidden: {', '.join(supplied)}")

    if config.agent_type != AgentType.CLAUDE or config.auth_mode != "host_session":
        return

    if not config.claude_config_dir:
        raise RuntimeError(
            "CLAUDE_CONFIG_DIR is not set for a Claude host_session worker: without it the CLI "
            "keeps ~/.claude.json in the container's ephemeral layer and loses it on restart. "
            "worker-manager must point it at the mounted host session directory (HOST_CLAUDE_DIR)."
        )

    config_dir = Path(config.claude_config_dir)
    if not config_dir.is_dir():
        raise RuntimeError(
            f"CLAUDE_CONFIG_DIR is not an existing directory: {config_dir}. "
            "The host Claude session directory (HOST_CLAUDE_DIR) is not mounted into this worker."
        )
    if not config_dir.is_mount():
        raise RuntimeError(
            f"CLAUDE_CONFIG_DIR is not a mounted host directory: {config_dir} belongs to the "
            "container's own filesystem, so the Claude config and session would be lost on "
            "restart. HOST_CLAUDE_DIR must be bind-mounted there."
        )
    if not os.access(config_dir, os.W_OK):
        raise RuntimeError(
            f"CLAUDE_CONFIG_DIR is not writable by the worker user: {config_dir}. "
            "The CLI cannot persist .claude.json there, so every run would start from empty state."
        )
