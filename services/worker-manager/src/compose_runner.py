import asyncio
import os
import subprocess
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger()

# Commands that trigger network injection
_NETWORK_INJECTION_COMMANDS = {"up", "run", "build"}

# Network override file written in the workspace
_NETWORK_OVERRIDE_FILENAME = ".codegen-network.yml"

# Ports override file that clears published ports (avoids host port conflicts)
_PORTS_OVERRIDE_FILENAME = ".codegen-ports.yml"

# Default compose files for service-template projects (under infra/)
_DEFAULT_COMPOSE_FILES = ["infra/compose.base.yml", "infra/compose.dev.yml"]


def _generate_ports_override(compose_files: list[Path]) -> str | None:
    """Generate a compose override that clears published ports for all services.

    Parses the compose files to find services with `ports` defined,
    then returns an override YAML that sets `ports: []` for each.
    This prevents host port conflicts when workers run on a host
    that already has those ports bound (e.g. orchestrator's own postgres/redis).

    Returns None if no services have ports defined.
    """
    services_with_ports: set[str] = set()
    for cf in compose_files:
        if not cf.exists():
            continue
        try:
            data = yaml.safe_load(cf.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        services = data.get("services")
        if not isinstance(services, dict):
            continue
        for svc_name, svc_config in services.items():
            if isinstance(svc_config, dict) and svc_config.get("ports"):
                services_with_ports.add(svc_name)

    if not services_with_ports:
        return None

    override = {"services": {svc: {"ports": []} for svc in sorted(services_with_ports)}}
    return yaml.dump(override, default_flow_style=False)


def _generate_network_override(worker_id: str) -> str:
    """Generate a compose network override that routes the default network to the worker dev network.

    Convention: compose files from service-template do NOT define custom networks,
    so all services use the implicit 'default' network. This override redirects it
    to the pre-created external dev network for the worker.

    Workers are on codegen_worker (isolated from orchestrator infra), so 'db'
    resolves only to the project's own postgres on dev_proj_<id>.
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
        workspace_dir: str | None = None,
    ) -> tuple[int, str, str]:
        """Run a docker compose command for a worker.

        Args:
            worker_id: The worker ID (used to scope project name and resolve paths).
            args: docker compose subcommand + flags (e.g. ["up", "-d", "db"]).
            cwd: Working directory relative to the worker's /workspace.
            timeout: Subprocess timeout in seconds.
            env: Additional environment variables to pass to the subprocess.
            workspace_dir: Explicit workspace path. When set, overrides the default
                           derivation from worker_id (needed when workspace is keyed
                           by project_id rather than worker_id).

        Returns:
            (exit_code, stdout, stderr)
        """
        if workspace_dir:
            worker_workspace = Path(workspace_dir)
        else:
            worker_workspace = self.workspace_base_path / worker_id / "workspace"
        if not worker_workspace.is_dir():
            raise ValueError(f"Workspace for worker '{worker_id}' does not exist: {worker_workspace}")
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

        # If user didn't pass -f, add default compose files explicitly.
        # All projects use service-template layout: infra/compose.base.yml + compose.dev.yml.
        # Must always be added because there's no docker-compose.yml for auto-discovery.
        default_file_args: list[str] = []
        if not file_args:
            for cf in _DEFAULT_COMPOSE_FILES:
                default_file_args.extend(["-f", cf])

        # Inject network override for commands that start containers.
        # The override file is placed in effective_cwd and referenced by absolute path
        # so it works regardless of --project-directory / compose file location.
        network_args: list[str] = []
        ports_args: list[str] = []
        if subcommand in _NETWORK_INJECTION_COMMANDS:
            override_path = effective_cwd / _NETWORK_OVERRIDE_FILENAME
            override_content = _generate_network_override(worker_id)
            override_path.write_text(override_content)
            abs_override = str(override_path)
            network_args = ["-f", abs_override]

            # Clear published ports to avoid host port conflicts.
            # Workers communicate via Docker DNS on the isolated network,
            # so published ports are unnecessary and would conflict with
            # the orchestrator's own postgres/redis.
            all_compose_files = (
                [effective_cwd / file_args[i + 1] for i in range(0, len(file_args), 2)]
                if file_args
                else [effective_cwd / cf for cf in _DEFAULT_COMPOSE_FILES]
            )
            ports_content = _generate_ports_override(all_compose_files)
            if ports_content:
                ports_path = effective_cwd / _PORTS_OVERRIDE_FILENAME
                ports_path.write_text(ports_content)
                ports_args = ["-f", str(ports_path)]

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
            *default_file_args,
            *network_args,
            *ports_args,
            *command_args,
        ]

        # Build environment: HOST_UID/GID + caller-supplied overrides.
        # Load the project's .env into the subprocess env so its values override
        # vars inherited from worker-manager (e.g. POSTGRES_* from the orchestrator).
        # Docker compose precedence: shell env > --env-file, so we must set them here.
        run_env = dict(os.environ)
        run_env["HOST_UID"] = "1000"
        run_env["HOST_GID"] = "1000"
        if dot_env.exists():
            for line in dot_env.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                if key:
                    run_env[key] = value
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

        try:
            result = await loop.run_in_executor(None, _run_subprocess)
        except subprocess.TimeoutExpired:
            raise ValueError(f"docker compose timed out after {timeout}s for worker '{worker_id}'")

        logger.info(
            "compose_run_complete",
            worker_id=worker_id,
            exit_code=result.returncode,
        )

        return result.returncode, result.stdout, result.stderr
