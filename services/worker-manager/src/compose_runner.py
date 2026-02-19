import asyncio
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Commands that trigger network injection
_NETWORK_INJECTION_COMMANDS = {"up", "run", "build"}

# Network override file written next to the compose file
_NETWORK_OVERRIDE_FILENAME = ".codegen-network.yml"


def _generate_network_override(worker_id: str) -> str:
    """Generate a compose network override that attaches services to the worker dev network."""
    network_name = f"dev_proj_{worker_id}"
    return f"networks:\n" f"  default:\n" f"    name: {network_name}\n" f"    external: true\n"


class ComposeRunner:
    """Runs docker compose as a subprocess on the host, scoped to a worker's workspace."""

    def __init__(self, workspace_base_path: str):
        self.workspace_base_path = Path(workspace_base_path)

    async def run(
        self,
        worker_id: str,
        args: list[str],
        cwd: str = ".",
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Run a docker compose command for a worker.

        Args:
            worker_id: The worker ID (used to scope project name and resolve paths).
            args: docker compose subcommand + flags (e.g. ["up", "-d", "db"]).
            cwd: Working directory relative to the worker's /workspace.
            timeout: Subprocess timeout in seconds.
            env: Additional environment variables to pass to the subprocess.

        Returns:
            (exit_code, stdout, stderr)
        """
        worker_workspace = self.workspace_base_path / worker_id / "workspace"
        worker_workspace_resolved = worker_workspace.resolve()

        # Resolve the cwd within the workspace
        try:
            effective_cwd = (worker_workspace / cwd).resolve()
            effective_cwd.relative_to(worker_workspace_resolved)
        except ValueError:
            raise ValueError(f"Path traversal detected: cwd '{cwd}' resolves outside workspace")

        project_name = f"worker_{worker_id}"

        # Determine subcommand for network injection
        subcommand = next((a for a in args if not a.startswith("-")), None)

        # Inject network override for commands that start containers
        extra_args: list[str] = []
        if subcommand in _NETWORK_INJECTION_COMMANDS:
            override_path = effective_cwd / _NETWORK_OVERRIDE_FILENAME
            override_content = _generate_network_override(worker_id)
            override_path.write_text(override_content)
            extra_args = [
                "-f",
                "docker-compose.yml",
                "-f",
                _NETWORK_OVERRIDE_FILENAME,
            ]

        cmd = [
            "docker",
            "compose",
            "--project-name",
            project_name,
            "--project-directory",
            str(effective_cwd),
            *extra_args,
            *args,
        ]

        # Build environment: HOST_UID/GID + caller-supplied overrides
        import os

        run_env = dict(os.environ)
        run_env["HOST_UID"] = "1000"
        run_env["HOST_GID"] = "1000"
        if env:
            run_env.update(env)

        logger.info(
            "compose_run",
            worker_id=worker_id,
            cmd=cmd,
            cwd=str(effective_cwd),
        )

        loop = asyncio.get_running_loop()

        def _run_subprocess():
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(effective_cwd),
                env=run_env,
                timeout=timeout,
            )
            return result

        result = await loop.run_in_executor(None, _run_subprocess)

        logger.info(
            "compose_run_complete",
            worker_id=worker_id,
            exit_code=result.returncode,
        )

        return result.returncode, result.stdout, result.stderr
