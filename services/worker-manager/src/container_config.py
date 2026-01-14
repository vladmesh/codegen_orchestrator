from dataclasses import dataclass
from typing import List, Optional, Dict, Any


@dataclass
class WorkerContainerConfig:
    """configuration for a worker container."""

    worker_id: str
    worker_type: str
    agent_type: str
    capabilities: List[str]
    auth_mode: str = "host_session"  # "host_session" or "api_key"
    host_claude_dir: Optional[str] = None
    api_key: Optional[str] = None

    def to_env_vars(self, redis_url: str, api_url: str) -> Dict[str, str]:
        """Generate environment variables for the container.

        Note: worker-wrapper uses WORKER_ prefix for pydantic-settings,
        so all config vars must have this prefix.
        """
        from shared.contracts.queues.worker import WorkerChannels

        env = {
            "WORKER_ID": self.worker_id,
            "WORKER_REDIS_URL": redis_url,  # worker-wrapper expects WORKER_ prefix
            "WORKER_API_URL": api_url,
            "WORKER_AGENT_TYPE": self.agent_type,
            "WORKER_TYPE": self.worker_type,
            "WORKER_CAPABILITIES": ",".join(self.capabilities),
            # Redis Stream Config (already have WORKER_ prefix)
            "WORKER_INPUT_STREAM": WorkerChannels.INPUT_PATTERN.value.format(worker_id=self.worker_id),
            "WORKER_OUTPUT_STREAM": WorkerChannels.OUTPUT_PATTERN.value.format(worker_id=self.worker_id),
            "WORKER_CONSUMER_GROUP": "worker_group",
            "WORKER_CONSUMER_NAME": self.worker_id,
        }

        if self.auth_mode == "api_key" and self.api_key:
            if self.agent_type == "factory":
                env["FACTORY_API_KEY"] = self.api_key
            else:
                env["ANTHROPIC_API_KEY"] = self.api_key

        return env

    def to_volume_mounts(self) -> Dict[str, Dict[str, str]]:
        """Generate volume mounts for the container."""
        volumes = {}

        # Mount Docker socket if DOCKER capability is requested
        if "DOCKER" in self.capabilities:
            volumes["/var/run/docker.sock"] = {"bind": "/var/run/docker.sock", "mode": "rw"}

        # Mount host session directory if in host_session mode
        if self.auth_mode == "host_session" and self.host_claude_dir:
            # Mount to /home/worker/.claude inside container
            volumes[self.host_claude_dir] = {"bind": "/home/worker/.claude", "mode": "rw"}

        return volumes

    def to_docker_run_kwargs(self, network_name: Optional[str] = None) -> Dict[str, Any]:
        """Generate kwargs for docker.containers.run().

        Args:
            network_name: Optional Docker network to attach container to.
                          If None, uses host networking (production default).
                          If provided, attaches to specified network (for DIND/testing).
        """
        kwargs = {
            "detach": True,
            "name": f"worker-{self.worker_id}",
            "hostname": f"worker-{self.worker_id}",
            # Standard limits
            "mem_limit": "2g",
            "cpu_period": 100000,
            "cpu_quota": 100000,  # 1 CPU
        }

        if network_name:
            # Attach to specific network (for DIND / integration tests)
            kwargs["network"] = network_name
        else:
            # Use host networking (production default)
            kwargs["network_mode"] = "host"

        return kwargs
