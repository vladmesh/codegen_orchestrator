import asyncio
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import structlog
import yaml

from .compose_validator import (
    CONTAINER_CREATING_COMMANDS,
    RESOURCE_IDENTITY_POLICY,
    VALUE_FLAGS,
    assert_permitted_build_shape,
    validate_command,
    validate_compose_file,
    validate_effective_compose,
)

logger = structlog.get_logger()

# Commands that trigger network injection
_NETWORK_INJECTION_COMMANDS = {"up", "run", "build"}

# Network override file written in the workspace
_NETWORK_OVERRIDE_FILENAME = ".codegen-network.yml"

_PLAN_DIRECTORY = ".compose-plans"
_DEFAULT_CPU_LIMIT = "1.0"
_DEFAULT_MEMORY_LIMIT = "512MiB"
_DOCKER_EXECUTABLE = Path(shutil.which("docker") or "/usr/bin/docker")

# Default compose files for service-template projects (under infra/)
_DEFAULT_COMPOSE_FILES = ["infra/compose.base.yml", "infra/compose.dev.yml"]

# These Compose v2.27.1 loader fields must have been consumed while compiling the
# manager-owned plan. Keeping one would let execution read a worker-selected
# source after the pre-resolution admission boundary. Build inputs are deliberately
# absent: they are execution inputs protected by the static source policy instead.
_SNAPSHOT_LOADER_DIRECTIVES = ("env_file", "extends", "label_file")

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
    project_directory: Path
    project_name: str
    snapshot_path: Path | None = None
    worker_id: str = ""


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


def _apply_effective_overrides(data: dict, worker_id: str) -> None:
    """Apply manager-owned resource identities, limits, and port policy to the resolved project."""
    services = data.get("services")
    if not isinstance(services, dict):
        raise ValueError("Resolved Compose configuration must contain services")
    RESOURCE_IDENTITY_POLICY.apply(data, worker_id)
    for name, service in services.items():
        if not isinstance(service, dict):
            raise ValueError(f"Service '{name}' must be a mapping")
        service["ports"] = []
        deploy = service.setdefault("deploy", {})
        if not isinstance(deploy, dict):
            continue
        resources = deploy.setdefault("resources", {})
        if not isinstance(resources, dict):
            continue
        limits = resources.get("limits")
        if limits is None:
            resources["limits"] = {"cpus": _DEFAULT_CPU_LIMIT, "memory": _DEFAULT_MEMORY_LIMIT}


def _write_snapshot(invocation: ComposeInvocation, data: dict, command_args: list[str]) -> ComposeInvocation:
    """Persist the validated resolved project outside the worker-writable workspace."""
    services = data.get("services")
    if not isinstance(services, dict):
        raise ValueError("Resolved Compose configuration must contain services")
    for service_name, service in services.items():
        if not isinstance(service, dict):
            raise ValueError(f"Service '{service_name}' must be a mapping")
        for directive in _SNAPSHOT_LOADER_DIRECTIVES:
            if directive in service:
                raise ValueError(f"Service '{service_name}': {directive} cannot be retained in an execution snapshot")
        assert_permitted_build_shape(service_name, service)
    RESOURCE_IDENTITY_POLICY.assert_snapshot(data, invocation.worker_id)
    plan_directory = invocation.snapshot_path.parent if invocation.snapshot_path else None
    if plan_directory is None:
        raise ValueError("Compose plan directory is unavailable")
    snapshot_path = plan_directory / "compose.resolved.yml"
    snapshot_path.write_text(yaml.safe_dump(data, sort_keys=True))
    snapshot_path.chmod(0o600)
    command = [
        str(_DOCKER_EXECUTABLE),
        "compose",
        "--project-name",
        invocation.project_name,
        "--project-directory",
        str(invocation.project_directory),
        "-f",
        str(snapshot_path),
        *command_args,
    ]
    return replace(invocation, command=command, snapshot_path=snapshot_path)


class ComposeRunner:
    """Runs docker compose as a subprocess on the host, scoped to a worker's workspace."""

    def __init__(self, workspace_base_path: str):
        self.workspace_base_path = Path(workspace_base_path)

    def _plan_directory(self, worker_id: str) -> Path:
        path = self.workspace_base_path / _PLAN_DIRECTORY / worker_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

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

    @staticmethod
    def _without_file_flags(args: list[str]) -> list[str]:
        result: list[str] = []
        index = 0
        while index < len(args):
            if args[index] in ("-f", "--file"):
                index += 2
                continue
            result.append(args[index])
            index += 1
        return result

    @staticmethod
    def _validate_source_tree(
        source: Path,
        workspace_path: Path,
        source_base: Path,
        seen: set[tuple[Path, Path]] | None = None,
    ) -> None:
        """Validate a Compose source with the path base Compose assigned to it."""
        seen = seen or set()
        try:
            source = source.resolve()
            source.relative_to(workspace_path)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Compose source cannot be resolved: {source}") from exc
        context = (source, source_base.resolve())
        if context in seen:
            return
        seen.add(context)
        try:
            content = source.read_text()
            source_result = validate_compose_file(
                content,
                source_file=source,
                workspace_path=workspace_path,
                project_directory=source_base,
            )
            data = yaml.safe_load(content)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
            raise ValueError(f"Compose source cannot be resolved: {source}: {exc}") from exc
        if not source_result.valid:
            raise ValueError("; ".join(source_result.errors))
        services = data.get("services", {}) if isinstance(data, dict) else {}
        if not isinstance(services, dict):
            return
        for service in services.values():
            extends = service.get("extends") if isinstance(service, dict) else None
            if isinstance(extends, dict) and isinstance(extends.get("file"), str):
                target = (source_base / extends["file"]).resolve()
                ComposeRunner._validate_source_tree(target, workspace_path, target.parent, seen)

    @staticmethod
    def _has_global_file_selection(args: list[str]) -> bool:
        """Return whether a worker selected a Compose file before its subcommand."""
        index = 0
        while index < len(args):
            arg = args[index]
            if arg in ("-f", "--file"):
                return True
            if arg in VALUE_FLAGS:
                index += 2
                continue
            if arg.startswith("-"):
                index += 1
                continue
            return False
        return False

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
        plan_directory = self._plan_directory(worker_id)

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

        compose_sources: list[Path] = []
        for compose_file in source_files:
            try:
                source = (effective_cwd / compose_file).resolve()
                source.relative_to(worker_workspace_resolved)
            except (OSError, ValueError) as exc:
                raise ValueError(f"Compose source cannot be resolved: {compose_file}") from exc
            if not source.is_file():
                raise ValueError(f"Compose source cannot be resolved: {compose_file}")
            compose_sources.append(source)
        project_directory = compose_sources[0].parent

        network_args: list[str] = []
        if subcommand in _NETWORK_INJECTION_COMMANDS:
            override_path = plan_directory / _NETWORK_OVERRIDE_FILENAME
            override_path.write_text(_generate_network_override(worker_id))
            for source in compose_sources:
                self._validate_source_tree(source, worker_workspace_resolved, project_directory)
            network_args = ["-f", str(override_path)]

        env_file_args: list[str] = []
        dot_env = worker_workspace_resolved / ".env"
        if dot_env.exists():
            env_file_args = ["--env-file", str(dot_env)]

        prefix = [
            str(_DOCKER_EXECUTABLE),
            "compose",
            "--project-name",
            project_name,
            "--project-directory",
            str(project_directory),
            *env_file_args,
            *file_args,
            *default_file_args,
            *network_args,
        ]
        run_env = {key: value for key in _INHERITED_ENV_VARS if (value := os.environ.get(key)) is not None}
        run_env["HOST_UID"] = "1000"
        run_env["HOST_GID"] = "1000"
        if env:
            raise ValueError("Caller-controlled Compose environment is not allowed")

        return ComposeInvocation(
            command=[*prefix, *command_args],
            config_command=[*prefix, "config", "--format", "json"],
            cwd=effective_cwd,
            env=run_env,
            source_files=source_files,
            workspace_path=worker_workspace_resolved,
            project_directory=project_directory,
            project_name=project_name,
            snapshot_path=plan_directory / "compose.resolved.yml",
            worker_id=worker_id,
        )

    def _prepare_recovery_invocation(
        self, worker_id: str, args: list[str], workspace_dir: str | None = None
    ) -> ComposeInvocation:
        """Build a project-bound cleanup command without reading a worker manifest."""
        command_result = validate_command(args)
        if not command_result.valid:
            raise ValueError("; ".join(command_result.errors))
        if self._has_global_file_selection(args):
            raise ValueError("Recovery commands do not allow worker-selected Compose files")
        workspace = Path(workspace_dir) if workspace_dir else self.workspace_base_path / worker_id / "workspace"
        if not workspace.is_dir():
            raise ValueError(f"Workspace for worker '{worker_id}' does not exist: {workspace}")
        plan_directory = self._plan_directory(worker_id)
        environment = {key: value for key in _INHERITED_ENV_VARS if (value := os.environ.get(key)) is not None}
        environment["HOST_UID"] = "1000"
        environment["HOST_GID"] = "1000"
        project_name = f"worker_{worker_id}"
        return ComposeInvocation(
            command=[
                str(_DOCKER_EXECUTABLE),
                "compose",
                "--project-name",
                project_name,
                "--project-directory",
                str(plan_directory),
                *args,
            ],
            config_command=[],
            cwd=plan_directory,
            env=environment,
            source_files=[],
            workspace_path=workspace.resolve(),
            project_directory=plan_directory,
            project_name=project_name,
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
            _apply_effective_overrides(data, worker_id)
            policy_result = validate_effective_compose(data, worker_id, invocation.workspace_path)
            if not policy_result.valid:
                raise ValueError("; ".join(policy_result.errors))
            invocation = _write_snapshot(invocation, data, self._without_file_flags(args))
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
        subcommand = self._subcommand(args)
        if subcommand in CONTAINER_CREATING_COMMANDS:
            if prepared is None:
                _, prepared = await self.inspect(
                    worker_id, args, cwd=cwd, timeout=timeout, env=env, workspace_dir=workspace_dir
                )
            invocation = prepared
        else:
            invocation = self._prepare_recovery_invocation(worker_id, args, workspace_dir)

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

        if subcommand == "down":
            shutil.rmtree(invocation.cwd, ignore_errors=True)

        return result.returncode, result.stdout, result.stderr
