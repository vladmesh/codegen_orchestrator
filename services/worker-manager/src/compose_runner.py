import asyncio
import os
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Commands that trigger network injection
_NETWORK_INJECTION_COMMANDS = {"up", "run", "build"}

# Network override file written in the workspace
_NETWORK_OVERRIDE_FILENAME = ".codegen-network.yml"


def _generate_network_override(worker_id: str) -> str:
    """Generate a compose network override that routes the default network to the worker dev network.

    Convention: compose files from service-template do NOT define custom networks,
    so all services use the implicit 'default' network. This override redirects it
    to the pre-created external dev network for the worker.
    """
    network_name = f"dev_proj_{worker_id}"
    return f"networks:\n  default:\n    name: {network_name}\n    external: true\n"


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

        # Determine subcommand for network injection (skip flag values like -f <file>)
        from .compose_validator import VALUE_FLAGS

        subcommand = None
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a in VALUE_FLAGS:
                skip_next = True
                continue
            if not a.startswith("-"):
                subcommand = a
                break

        # Split user args into file-flags and command args.
        # File flags are placed first, then network override, then the command.
        file_args: list[str] = []
        command_args: list[str] = list(args)
        i = 0
        while i < len(command_args):
            if command_args[i] in ("-f", "--file"):
                file_args.extend(command_args[i : i + 2])
                command_args = command_args[:i] + command_args[i + 2 :]
            else:
                i += 1

        # Inject network override for commands that start containers.
        # The override file is placed in effective_cwd and referenced by absolute path
        # so it works regardless of --project-directory / compose file location.
        network_args: list[str] = []
        if subcommand in _NETWORK_INJECTION_COMMANDS:
            override_path = effective_cwd / _NETWORK_OVERRIDE_FILENAME
            override_content = _generate_network_override(worker_id)
            override_path.write_text(override_content)
            abs_override = str(override_path)
            network_args = ["-f", abs_override]

            # If user didn't pass -f, add default docker-compose.yml explicitly
            # (because adding any -f disables auto-discovery)
            if not file_args:
                network_args = ["-f", "docker-compose.yml"] + network_args

        # NOTE: We don't pass --project-directory. Docker compose uses the directory
        # of the first compose file by default, which preserves relative env_file
        # paths inside compose manifests (e.g. env_file: ../.env).
        # The subprocess runs with cwd=effective_cwd for default file discovery.
        #
        # We pass --env-file if a .env exists in the workspace root (docker compose
        # auto-discovers .env from the project directory, which may differ from cwd).
        env_file_args: list[str] = []
        dot_env = worker_workspace_resolved / ".env"
        if dot_env.exists():
            env_file_args = ["--env-file", str(dot_env)]

        cmd = [
            "docker",
            "compose",
            "--project-name",
            project_name,
            *env_file_args,
            *file_args,
            *network_args,
            *command_args,
        ]

        # Build environment: HOST_UID/GID + caller-supplied overrides
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
