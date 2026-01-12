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
        """Generate environment variables for the container."""
        env = {
            "WORKER_ID": self.worker_id,
            "REDIS_URL": redis_url,
            "API_URL": api_url,
            "AGENT_TYPE": self.agent_type,
            "WORKER_TYPE": self.worker_type,
            "CAPABILITIES": ",".join(self.capabilities),
        }

        if self.auth_mode == "api_key" and self.api_key:
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

    def to_docker_run_kwargs(self) -> Dict[str, Any]:
        """Generate kwargs for docker.containers.run()."""
        # Base config
        return {
            "detach": True,
            "network_mode": "host",  # Using host networking for now as per design
            "name": f"worker-{self.worker_id}",
            "hostname": f"worker-{self.worker_id}",
            # Standard limits can be added here
            "mem_limit": "2g",
            "cpu_period": 100000,
            "cpu_quota": 100000,  # 1 CPU
        }
