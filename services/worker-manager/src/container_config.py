from dataclasses import dataclass
from typing import List, Optional, Dict, Any

from shared.contracts.vocab import AgentType

# The host Claude session directory is mounted here, and the Claude CLI is told
# to keep its whole config there via CLAUDE_CONFIG_DIR. Without that variable the
# CLI writes ~/.claude.json into the container's ephemeral layer while its backups
# land in the mounted directory, so every run starts from empty state.
CLAUDE_CONFIG_DIR = "/home/worker/.claude"
WORKER_PIDS_LIMIT = 256


@dataclass
class WorkerContainerConfig:
    """configuration for a worker container."""

    worker_id: str
    worker_type: str
    agent_type: AgentType
    capabilities: List[str]
    auth_mode: str = "host_session"  # "host_session" or "api_key"
    host_claude_dir: Optional[str] = None
    host_codex_home: Optional[str] = None
    api_key: Optional[str] = None
    workspace_host_path: Optional[str] = None
    transcript_host_path: Optional[str] = None
    transcript_max_bytes: int = 5 * 1024 * 1024

    def to_env_vars(
        self,
        broker_url: str | None = None,
        broker_token: str | None = None,
        subprocess_timeout_seconds: int = 300,
        *,
        redis_url: str | None = None,
        api_url: str | None = None,
        worker_manager_url: str | None = None,
    ) -> Dict[str, str]:
        """Generate environment variables for the container.

        Note: worker-wrapper uses WORKER_ prefix for pydantic-settings,
        so all config vars must have this prefix.
        """
        legacy_transport = not broker_url or not broker_token
        if legacy_transport:
            # Compatibility for removed tests and external callers during the
            # migration. WorkerManager never takes this branch in production.
            if not redis_url or not api_url:
                raise ValueError("broker_url and broker_token are required")

        env = {
            "WORKER_ID": self.worker_id,
            "WORKER_AGENT_TYPE": self.agent_type,
            "WORKER_TYPE": self.worker_type,
            "WORKER_CAPABILITIES": ",".join(self.capabilities),
            "WORKER_SUBPROCESS_TIMEOUT_SECONDS": str(subprocess_timeout_seconds),
            "WORKER_TRANSCRIPT_DIR": "/artifacts/worker-transcripts",
            "WORKER_TRANSCRIPT_MAX_BYTES": str(self.transcript_max_bytes),
            # The worker validates its own agent state against the mode it was
            # created with: host_session needs a mounted session, api_key does not.
            "WORKER_AUTH_MODE": self.auth_mode,
        }
        if legacy_transport:
            env.update({"WORKER_REDIS_URL": redis_url, "WORKER_API_URL": api_url})
            if worker_manager_url:
                env["WORKER_MANAGER_URL"] = worker_manager_url
        else:
            env.update({"WORKER_BROKER_URL": broker_url, "WORKER_BROKER_TOKEN": broker_token})

        if self.agent_type == AgentType.CLAUDE:
            env["CLAUDE_CONFIG_DIR"] = CLAUDE_CONFIG_DIR

        if self.agent_type == AgentType.CODEX:
            env["CODEX_HOME"] = "/home/worker/.codex"

        if self.auth_mode == "api_key" and self.api_key:
            if self.agent_type == AgentType.FACTORY:
                env["FACTORY_API_KEY"] = self.api_key
            elif self.agent_type == AgentType.CODEX:
                env["CODEX_API_KEY"] = self.api_key
            else:
                env["ANTHROPIC_API_KEY"] = self.api_key

        return env

    def to_volume_mounts(self) -> Dict[str, Dict[str, str]]:
        """Generate volume mounts for the container."""
        volumes = {}

        # Mount host session directory if in host_session mode
        if self.auth_mode == "host_session" and self.agent_type == AgentType.CLAUDE and self.host_claude_dir:
            # Mount to /home/worker/.claude inside container — the same directory
            # CLAUDE_CONFIG_DIR points at, so config and session share one owner.
            volumes[self.host_claude_dir] = {"bind": CLAUDE_CONFIG_DIR, "mode": "rw"}

        if self.auth_mode == "host_session" and self.agent_type == AgentType.CODEX and self.host_codex_home:
            volumes[self.host_codex_home] = {
                "bind": "/home/worker/.codex",
                "mode": "rw",
            }

        # Mount workspace directory if provided
        if self.workspace_host_path:
            volumes[self.workspace_host_path] = {"bind": "/workspace", "mode": "rw"}

        if self.transcript_host_path:
            volumes[self.transcript_host_path] = {
                "bind": "/artifacts/worker-transcripts",
                "mode": "rw",
            }

        # Shared uv cache for fast package installs across workers
        volumes["uv-cache"] = {"bind": "/home/worker/.cache/uv", "mode": "rw"}

        return volumes

    def to_docker_run_kwargs(
        self,
        network_name: Optional[str] = None,
        *,
        allow_host_network: bool = False,
    ) -> Dict[str, Any]:
        """Generate kwargs for docker.containers.run().

        Args:
            network_name: Dedicated Docker network to attach the container to.
            allow_host_network: Test-only compatibility switch for DinD.
        """
        if not network_name:
            raise ValueError("A dedicated Docker network is required for coding workers")

        if network_name == "host" and not allow_host_network:
            raise ValueError("Coding workers cannot use host networking")

        kwargs = {
            "detach": True,
            "name": f"worker-{self.worker_id}",
            "hostname": f"worker-{self.worker_id}",
            "mem_limit": self._mem_limit(),
            "cpu_period": 100000,
            "cpu_quota": 100000,  # 1 CPU
            "pids_limit": WORKER_PIDS_LIMIT,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
        }

        if network_name == "host":
            kwargs["network_mode"] = "host"
        else:
            kwargs["network"] = network_name

        return kwargs

    def _mem_limit(self) -> str:
        """Return the Docker memory limit for this worker agent."""
        if self.agent_type in {AgentType.CLAUDE, AgentType.FACTORY, AgentType.CODEX}:
            return "4g"
        return "2g"
