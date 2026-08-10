import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog
import yaml

from .compose_validator import (
    CONTAINER_CREATING_COMMANDS,
    MAX_CPU_LIMIT,
    MAX_MEMORY_LIMIT_BYTES,
    VALUE_FLAGS,
    validate_command,
    validate_effective_compose,
)

logger = structlog.get_logger()

# Commands that trigger network injection
_NETWORK_INJECTION_COMMANDS = {"up", "run", "build"}

# Network override file written in the workspace
_NETWORK_OVERRIDE_FILENAME = ".codegen-network.yml"

# Ports override file that clears published ports (avoids host port conflicts)
_PORTS_OVERRIDE_FILENAME = ".codegen-ports.yml"

# Limits override written in the workspace for every container-creating command.
_LIMITS_OVERRIDE_FILENAME = ".codegen-limits.yml"

# Default compose files for service-template projects (under infra/)
_DEFAULT_COMPOSE_FILES = ["infra/compose.base.yml", "infra/compose.dev.yml"]

# Environment passed to `docker compose`. The compose manifest belongs to the
# agent and compose interpolates ${VAR} out of this environment into it, with the
# result handed back to the agent as command output. Inheriting worker-manager's
# own environment would therefore publish the orchestrator .env, so the subprocess
# gets an explicit list instead: what Docker itself needs, plus the project's .env.
_INHERITED_ENV_VARS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
    "DOCKER_TLS_VERIFY",
    "DOCKER_BUILDKIT",
    "COMPOSE_DOCKER_CLI_BUILD",
)


@dataclass(frozen=True)
class ComposeInvocation:
    """The single resolved Compose invocation used for inspection and execution."""

    command: list[str]
    config_command: list[str]
    cwd: Path
    env: dict[str, str]
    source_files: list[str]
    workspace_path: Path


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
        if not cf.is_file():
            raise ValueError(f"Compose source is unavailable: {cf}")
        try:
            data = yaml.safe_load(cf.read_text())
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"Compose source cannot be resolved: {cf}: {exc}") from exc
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

    # Use !reset tag so Docker Compose v2 clears ports instead of merging.
    # yaml.dump doesn't support custom tags, so we build the YAML manually.
    lines = ["services:"]
    for svc in sorted(services_with_ports):
        lines.append(f"  {svc}:")
        lines.append("    ports: !reset []")
    return "\n".join(lines) + "\n"


def _generate_network_override(worker_id: str) -> str:
    """Generate a Compose override that routes the default network to the worker dev network.

    Convention: compose files from service-template do NOT define custom networks,
    so all services use the implicit 'default' network. This override redirects it
    to the pre-created external dev network for the worker.

    Workers are on codegen_worker (isolated from orchestrator infra), so 'db'
    resolves only to the project's own postgres on dev_proj_<id>.
    """
    network_name = f"dev_proj_{worker_id}"
    return f"networks:\n  default:\n    name: {network_name}\n    external: true\n"


def _generate_limits_override(compose_files: list[Path]) -> str:
    """Generate bounded resource limits for every service selected for execution."""
    service_names: set[str] = set()
    for compose_file in compose_files:
        if not compose_file.is_file():
            raise ValueError(f"Compose source is unavailable: {compose_file}")
        try:
            data = yaml.safe_load(compose_file.read_text())
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"Compose source cannot be resolved: {compose_file}: {exc}") from exc
        if not isinstance(data, dict):
            continue
        services = data.get("services")
        if isinstance(services, dict):
            service_names.update(str(name) for name, config in services.items() if isinstance(config, dict))
    if not service_names:
        raise ValueError("Compose sources contain no services for resource limits")

    lines = ["services:"]
    for service_name in sorted(service_names):
        lines.extend(
            [
                f"  {service_name}:",
                "    deploy:",
                "      resources:",
                "        limits:",
                f'          cpus: "{MAX_CPU_LIMIT}"',
                f'          memory: "{MAX_MEMORY_LIMIT_BYTES}"',
            ]
        )
    return "\n".join(lines) + "\n"


class ComposeRunner:
    """Runs docker compose as a subprocess on the host, scoped to a worker's workspace."""

    def __init__(self, workspace_base_path: str):
        self.workspace_base_path = Path(workspace_base_path)

    @staticmethod
    def _subcommand(args: list[str]) -> str | None:
        skip_next = False
        for arg in args:
            if skip_next:
                skip_next = False
                continue
            if arg in VALUE_FLAGS:
                skip_next = True
                continue
            if not arg.startswith("-"):
                return arg
        return None

    def _prepare_invocation(
        self,
        worker_id: str,
        args: list[str],
        cwd: str = ".",
        timeout: int = 120,
        env: dict[str, str] | None = None,
        workspace_dir: str | None = None,
    ) -> ComposeInvocation:
        """Resolve the fixed Compose scope shared by validation and execution."""
        command_result = validate_command(args)
        if not command_result.valid:
            raise ValueError("; ".join(command_result.errors))
        if workspace_dir:
            worker_workspace = Path(workspace_dir)
        else:
            worker_workspace = self.workspace_base_path / worker_id / "workspace"
        if not worker_workspace.is_dir():
            raise ValueError(f"Workspace for worker '{worker_id}' does not exist: {worker_workspace}")
        worker_workspace_resolved = worker_workspace.resolve()

        try:
            effective_cwd = (worker_workspace / cwd).resolve()
            effective_cwd.relative_to(worker_workspace_resolved)
        except ValueError as exc:
            raise ValueError(f"Path traversal detected: cwd '{cwd}' resolves outside workspace") from exc
        if not effective_cwd.is_dir():
            raise ValueError(f"Compose cwd does not exist: {effective_cwd}")

        project_name = f"worker_{worker_id}"

        subcommand = self._subcommand(args)

        file_args: list[str] = []
        command_args: list[str] = list(args)
        i = 0
        while i < len(command_args):
            if command_args[i] in ("-f", "--file"):
                if i + 1 >= len(command_args):
                    raise ValueError("Compose file flag requires a path")
                file_args.extend(command_args[i : i + 2])
                command_args = command_args[:i] + command_args[i + 2 :]
            else:
                i += 1

        default_file_args: list[str] = []
        source_files: list[str]
        if file_args:
            source_files = [file_args[i + 1] for i in range(0, len(file_args), 2)]
        else:
            source_files = list(_DEFAULT_COMPOSE_FILES)
            for compose_file in source_files:
                default_file_args.extend(["-f", compose_file])

        network_args: list[str] = []
        ports_args: list[str] = []
        limits_args: list[str] = []
        if subcommand in _NETWORK_INJECTION_COMMANDS:
            override_path = effective_cwd / _NETWORK_OVERRIDE_FILENAME
            override_path.write_text(_generate_network_override(worker_id))
            network_args = ["-f", str(override_path)]

            compose_sources = [effective_cwd / compose_file for compose_file in source_files]
            ports_content = _generate_ports_override(compose_sources)
            if ports_content:
                ports_path = effective_cwd / _PORTS_OVERRIDE_FILENAME
                ports_path.write_text(ports_content)
                ports_args = ["-f", str(ports_path)]
            limits_path = effective_cwd / _LIMITS_OVERRIDE_FILENAME
            limits_path.write_text(_generate_limits_override(compose_sources))
            limits_args = ["-f", str(limits_path)]

        env_file_args: list[str] = []
        dot_env = worker_workspace_resolved / ".env"
        if dot_env.exists():
            env_file_args = ["--env-file", str(dot_env)]

        prefix = [
            "docker",
            "compose",
            "--project-name",
            project_name,
            *env_file_args,
            *file_args,
            *default_file_args,
            *network_args,
            *ports_args,
            *limits_args,
        ]
        run_env = {key: value for key in _INHERITED_ENV_VARS if (value := os.environ.get(key)) is not None}
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

        return ComposeInvocation(
            command=[*prefix, *command_args],
            config_command=[*prefix, "config", "--format", "json"],
            cwd=effective_cwd,
            env=run_env,
            source_files=source_files,
            workspace_path=worker_workspace_resolved,
        )

    async def inspect(
        self,
        worker_id: str,
        args: list[str],
        cwd: str = ".",
        timeout: int = 120,
        env: dict[str, str] | None = None,
        workspace_dir: str | None = None,
    ) -> tuple[dict, ComposeInvocation]:
        """Resolve the exact execution invocation without creating containers."""
        invocation = self._prepare_invocation(
            worker_id, args, cwd=cwd, timeout=timeout, env=env, workspace_dir=workspace_dir
        )
        loop = asyncio.get_running_loop()

        def _run_config():
            return subprocess.run(
                invocation.config_command,
                capture_output=True,
                check=False,
                text=True,
                cwd=str(invocation.cwd),
                env=invocation.env,
                timeout=timeout,
            )

        try:
            result = await loop.run_in_executor(None, _run_config)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"docker compose config timed out after {timeout}s for worker '{worker_id}'") from exc
        except OSError as exc:
            raise ValueError(f"docker compose config is unavailable: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown resolution error"
            raise ValueError(f"docker compose config could not resolve the project: {detail}")
        try:
            data = yaml.safe_load(result.stdout)
        except yaml.YAMLError as exc:
            raise ValueError(f"docker compose config returned invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("docker compose config returned no project mapping")  # noqa: TRY004
        if self._subcommand(args) in CONTAINER_CREATING_COMMANDS:
            policy_result = validate_effective_compose(data, worker_id, invocation.workspace_path)
            if not policy_result.valid:
                raise ValueError("; ".join(policy_result.errors))
        return data, invocation

    async def run(
        self,
        worker_id: str,
        args: list[str],
        cwd: str = ".",
        timeout: int = 120,
        env: dict[str, str] | None = None,
        workspace_dir: str | None = None,
        prepared: ComposeInvocation | None = None,
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
        if prepared is None and self._subcommand(args) in CONTAINER_CREATING_COMMANDS:
            _, prepared = await self.inspect(
                worker_id, args, cwd=cwd, timeout=timeout, env=env, workspace_dir=workspace_dir
            )
        invocation = prepared or self._prepare_invocation(
            worker_id, args, cwd=cwd, timeout=timeout, env=env, workspace_dir=workspace_dir
        )

        logger.info(
            "compose_run",
            worker_id=worker_id,
            cmd=invocation.command,
            cwd=str(invocation.cwd),
        )

        loop = asyncio.get_running_loop()

        def _run_subprocess():
            result = subprocess.run(
                invocation.command,
                capture_output=True,
                check=False,
                text=True,
                cwd=str(invocation.cwd),
                env=invocation.env,
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
