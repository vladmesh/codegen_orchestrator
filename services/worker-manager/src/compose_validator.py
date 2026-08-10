"""Fail-closed policy checks for worker-scoped Docker Compose projects."""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ALLOWED_COMMANDS = {"up", "down", "build", "run", "ps", "logs", "stop"}
CONTAINER_CREATING_COMMANDS = {"up", "build", "run"}
# Flags that consume the next argument as a value (skip it when scanning for subcommand).
VALUE_FLAGS = {
    "-f",
    "--file",
    "--project-directory",
    "--project-name",
    "--env-file",
    "-p",
    "--profile",
}
_SCOPE_OVERRIDE_FLAGS = {"--project-directory", "--project-name", "--env-file", "-p", "--network"}
_GLOBAL_FILE_FLAGS = {"-f", "--file"}
_SAFE_COMMAND_FLAGS = {
    "up": {"-d", "--detach", "--wait", "--build", "--no-build"},
    "build": {"--no-cache", "--pull"},
    "run": set(),
    "down": {"-v", "--volumes", "--remove-orphans"},
    "ps": {"-a", "--all", "--quiet", "-q"},
    "logs": {"-f", "--follow", "--no-color", "--timestamps"},
    "stop": set(),
}
_SAFE_VALUE_COMMAND_FLAGS = {"up": {"--wait-timeout"}, "logs": {"--tail", "--since", "--until"}}

# Generated projects are allowed at most the same envelope as a capability worker.
# Compose accepts CPU values as decimal core counts and memory as bytes or an IEC/SI
# unit such as 512M or 1GiB.
MAX_CPU_LIMIT = 3.0
MAX_MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
_MEMORY_LIMIT_RE = re.compile(r"^(?P<amount>\d+(?:\.\d+)?)(?P<unit>[kKmMgGtT](?:i)?[bB]?)?$")


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def _flag_is_set(arg: str, flag: str) -> bool:
    return arg == flag or arg.startswith(f"{flag}=") or (flag == "-p" and arg.startswith("-p") and arg != "-p")


def _volume_parts(volume: Any) -> tuple[str, str, str]:
    if isinstance(volume, str):
        parts = volume.split(":")
        return (parts[0] if len(parts) > 1 else "", parts[1] if len(parts) > 1 else "", "bind")
    if isinstance(volume, dict):
        return (
            str(volume.get("source", "")),
            str(volume.get("target", "")),
            str(volume.get("type", "volume")),
        )
    return "", "", ""


def _is_socket_path(path: str) -> bool:
    lowered = path.lower()
    return "docker.sock" in lowered or "compose.sock" in lowered


def _is_within_workspace(source: str, workspace_path: Path) -> bool:
    try:
        Path(source).resolve().relative_to(workspace_path.resolve())
    except (OSError, ValueError):
        return False
    return True


def _validate_volumes(
    service_name: str,
    service_config: dict[str, Any],
    errors: list[str],
    *,
    workspace_path: Path | None = None,
    resolved: bool = False,
) -> None:
    volumes = service_config.get("volumes", [])
    if not isinstance(volumes, list):
        errors.append(f"Service '{service_name}': volumes must be a list")
        return
    for volume in volumes:
        source, target, volume_type = _volume_parts(volume)
        if _is_socket_path(source) or _is_socket_path(target):
            errors.append(f"Service '{service_name}': Docker or Compose socket mount is not allowed")
        if volume_type != "bind":
            continue
        if not resolved:
            if source.startswith("/"):
                errors.append(f"Service '{service_name}': absolute bind mount source '{source}' is not allowed")
            continue
        if not source.startswith("/") or workspace_path is None or not _is_within_workspace(source, workspace_path):
            errors.append(f"Service '{service_name}': absolute bind mount source '{source}' is not allowed")


def _validate_named_volumes(data: dict[str, Any], errors: list[str]) -> None:
    volumes = data.get("volumes", {})
    if volumes is None:
        return
    if not isinstance(volumes, dict):
        errors.append("Resolved Compose volumes must be a mapping")
        return
    for volume_name, volume_config in volumes.items():
        if volume_config is None:
            continue
        if not isinstance(volume_config, dict):
            errors.append(f"Volume '{volume_name}' must be a mapping")
            continue
        if volume_config.get("external"):
            errors.append(f"Volume '{volume_name}': external volumes are not allowed")
        driver = volume_config.get("driver", "local")
        if driver != "local":
            errors.append(f"Volume '{volume_name}': only the default local driver is allowed")
        driver_opts = volume_config.get("driver_opts")
        if driver_opts:
            errors.append(f"Volume '{volume_name}': driver_opts are not allowed")


def _validate_file_sources(kind: str, definitions: Any, workspace_path: Path | None, errors: list[str]) -> None:
    if definitions is None:
        return
    if not isinstance(definitions, dict):
        errors.append(f"Resolved Compose {kind} must be a mapping")
        return
    for name, definition in definitions.items():
        if not isinstance(definition, dict):
            errors.append(f"{kind.title()} '{name}' must be a mapping")
            continue
        if definition.get("external"):
            errors.append(f"{kind.title()} '{name}': external sources are not allowed")
        source = definition.get("file")
        if source and (
            not isinstance(source, str) or workspace_path is None or not _is_within_workspace(source, workspace_path)
        ):
            errors.append(f"{kind.title()} '{name}': file source must remain within the worker workspace")


def _validate_build(service_name: str, config: dict[str, Any], workspace_path: Path | None, errors: list[str]) -> None:
    build = config.get("build")
    if build is None:
        return
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        errors.append(f"Service '{service_name}': build must be a mapping")
        return
    for key in ("context", "dockerfile"):
        value = build.get(key)
        if value and (
            not isinstance(value, str) or workspace_path is None or not _is_within_workspace(value, workspace_path)
        ):
            errors.append(f"Service '{service_name}': build.{key} must remain within the worker workspace")
    for key in ("additional_contexts", "secrets", "ssh"):
        if build.get(key):
            errors.append(f"Service '{service_name}': build.{key} is not supported")


def _memory_bytes(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if not isinstance(value, str):
        return None
    match = _MEMORY_LIMIT_RE.fullmatch(value.strip())
    if not match:
        return None
    amount = float(match.group("amount"))
    if amount <= 0:
        return None
    unit = (match.group("unit") or "").lower()
    factors = {
        "": 1,
        "k": 1000,
        "kb": 1000,
        "m": 1000**2,
        "mb": 1000**2,
        "g": 1000**3,
        "gb": 1000**3,
        "t": 1000**4,
        "tb": 1000**4,
        "ki": 1024,
        "kib": 1024,
        "mi": 1024**2,
        "mib": 1024**2,
        "gi": 1024**3,
        "gib": 1024**3,
        "ti": 1024**4,
        "tib": 1024**4,
    }
    factor = factors.get(unit)
    return int(amount * factor) if factor is not None else None


def _validate_resource_limits(service_name: str, service_config: dict[str, Any], errors: list[str]) -> None:
    try:
        limits = service_config["deploy"]["resources"]["limits"]
    except (KeyError, TypeError):
        errors.append(f"Service '{service_name}': deploy.resources.limits with CPU and memory limits is required")
        return
    if not isinstance(limits, dict):
        errors.append(f"Service '{service_name}': deploy.resources.limits must be a mapping")
        return
    cpus = limits.get("cpus")
    try:
        cpu_limit = float(cpus)
    except (TypeError, ValueError):
        cpu_limit = 0
    if not math.isfinite(cpu_limit) or cpu_limit <= 0 or cpu_limit > MAX_CPU_LIMIT:
        errors.append(f"Service '{service_name}': CPU limit must be greater than 0 and at most {MAX_CPU_LIMIT}")
    memory_limit = _memory_bytes(limits.get("memory"))
    if memory_limit is None or memory_limit > MAX_MEMORY_LIMIT_BYTES:
        errors.append(
            f"Service '{service_name}': memory limit must be a positive value at most {MAX_MEMORY_LIMIT_BYTES} bytes"
        )


def validate_command(args: list[str]) -> ValidationResult:
    """Validate Compose commands without allowing the worker to alter its scope."""
    errors: list[str] = []
    index = 0
    subcommand = None
    while index < len(args):
        arg = args[index]
        if arg in _GLOBAL_FILE_FLAGS:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                errors.append("Compose file flag requires a separate path")
                break
            index += 2
            continue
        if arg.startswith("-"):
            if any(_flag_is_set(arg, flag) for flag in _SCOPE_OVERRIDE_FLAGS):
                errors.append(f"Flag '{arg}' cannot override the worker Compose scope")
            elif arg.startswith("--file=") or (arg.startswith("-f") and arg != "-f"):
                errors.append(f"Flag '{arg}' must use a separate Compose file path")
            else:
                errors.append(f"Global flag '{arg}' is not allowed")
            index += 1
            continue
        subcommand = arg
        index += 1
        break

    if subcommand is None:
        errors.append("No subcommand found in args")
        return ValidationResult(valid=False, errors=errors)
    if subcommand not in ALLOWED_COMMANDS:
        errors.append(f"Command '{subcommand}' is not allowed. Allowed: {sorted(ALLOWED_COMMANDS)}")
        return ValidationResult(valid=False, errors=errors)

    safe_flags = _SAFE_COMMAND_FLAGS[subcommand]
    safe_value_flags = _SAFE_VALUE_COMMAND_FLAGS.get(subcommand, set())
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            index += 1
            continue
        if arg in safe_flags:
            index += 1
            continue
        if arg in safe_value_flags:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                errors.append(f"Flag '{arg}' requires a value")
            index += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in safe_value_flags):
            index += 1
            continue
        if any(_flag_is_set(arg, flag) for flag in _SCOPE_OVERRIDE_FLAGS):
            errors.append(f"Flag '{arg}' cannot override the worker Compose scope")
        else:
            errors.append(f"Flag '{arg}' is not allowed for '{subcommand}'")
        index += 1

    return ValidationResult(valid=not errors, errors=errors)


def _source_path(value: Any, source_file: Path, workspace_path: Path, errors: list[str], label: str) -> None:
    if not isinstance(value, str) or not value:
        errors.append(f"{label} must be a non-empty workspace path")
        return
    try:
        resolved = (source_file.parent / value).resolve()
        resolved.relative_to(workspace_path.resolve())
    except (OSError, ValueError):
        errors.append(f"{label} must remain within the worker workspace")


def _source_path_values(value: Any, source_file: Path, workspace_path: Path, errors: list[str], label: str) -> None:
    if isinstance(value, str):
        _source_path(value, source_file, workspace_path, errors, label)
        return
    if not isinstance(value, list):
        errors.append(f"{label} must be a workspace path or list of workspace paths")
        return
    for item in value:
        if isinstance(item, dict):
            item = item.get("path")
        _source_path(item, source_file, workspace_path, errors, label)


def _validate_source_build(
    service_name: str, config: dict[str, Any], source_file: Path, workspace_path: Path, errors: list[str]
) -> None:
    build = config.get("build")
    if build is None:
        return
    if isinstance(build, str):
        _source_path(build, source_file, workspace_path, errors, f"Service '{service_name}': build context")
        return
    if not isinstance(build, dict):
        errors.append(f"Service '{service_name}': build must be a mapping")
        return
    context = build.get("context", ".")
    _source_path(context, source_file, workspace_path, errors, f"Service '{service_name}': build context")
    if "dockerfile" in build:
        # Dockerfile is relative to the build context, unlike other Compose paths.
        if isinstance(context, str) and isinstance(build["dockerfile"], str):
            _source_path(
                str(Path(context) / build["dockerfile"]),
                source_file,
                workspace_path,
                errors,
                f"Service '{service_name}': build dockerfile",
            )
        else:
            errors.append(f"Service '{service_name}': build dockerfile must be a workspace path")
    for key in ("additional_contexts", "secrets", "ssh"):
        if key not in build:
            continue
        value = build[key]
        if isinstance(value, dict):
            values = list(value.values())
        else:
            values = value
        _source_path_values(values, source_file, workspace_path, errors, f"Service '{service_name}': build {key}")


def validate_compose_file(
    content: str, *, source_file: Path | None = None, workspace_path: Path | None = None
) -> ValidationResult:
    """Validate an individual selected Compose source before configuration resolution.

    Compose reads ``env_file`` and ``extends.file`` while resolving configuration, so
    source-only paths are checked before the CLI can read beyond the workspace.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return ValidationResult(valid=False, errors=[f"Invalid YAML: {exc}"])
    if not isinstance(data, dict):
        return ValidationResult(valid=False, errors=["Compose file must be a YAML mapping"])
    services = data.get("services", {})
    if not isinstance(services, dict):
        return ValidationResult(valid=False, errors=["'services' must be a mapping"])

    errors: list[str] = []
    for service_name, service_config in services.items():
        if not isinstance(service_config, dict):
            errors.append(f"Service '{service_name}' must be a mapping")
            continue
        _validate_volumes(str(service_name), service_config, errors)
        if source_file is None or workspace_path is None:
            continue
        name = str(service_name)
        if "env_file" in service_config:
            _source_path_values(
                service_config["env_file"], source_file, workspace_path, errors, f"Service '{name}': env_file"
            )
        extends = service_config.get("extends")
        if isinstance(extends, dict) and "file" in extends:
            _source_path(extends["file"], source_file, workspace_path, errors, f"Service '{name}': extends file")
        elif extends is not None and not isinstance(extends, dict):
            errors.append(f"Service '{name}': extends must be a mapping")
        _validate_source_build(name, service_config, source_file, workspace_path, errors)

    if source_file is not None and workspace_path is not None:
        if data.get("include"):
            errors.append("Compose include is not supported")
        for kind in ("secrets", "configs"):
            definitions = data.get(kind, {})
            if not isinstance(definitions, dict):
                errors.append(f"{kind.title()} must be a mapping")
                continue
            for name, definition in definitions.items():
                if not isinstance(definition, dict):
                    errors.append(f"{kind.title()} '{name}' must be a mapping")
                    continue
                if definition.get("external"):
                    errors.append(f"{kind.title()} '{name}': external sources are not allowed")
                if "file" in definition:
                    _source_path(
                        definition["file"], source_file, workspace_path, errors, f"{kind.title()} '{name}': file"
                    )
    return ValidationResult(valid=not errors, errors=errors)


def validate_effective_compose(data: Any, worker_id: str, workspace_path: Path | None = None) -> ValidationResult:
    """Validate Docker Compose's fully resolved JSON for a container-creating command."""
    if not isinstance(data, dict):
        return ValidationResult(valid=False, errors=["Resolved Compose configuration must be a mapping"])
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        return ValidationResult(valid=False, errors=["Resolved Compose configuration must contain services"])

    expected_network = f"dev_proj_{worker_id}"
    errors: list[str] = []
    networks = data.get("networks")
    if not isinstance(networks, dict) or set(networks) != {"default"}:
        errors.append("Resolved Compose configuration may use only the worker default network")
    else:
        default_network = networks["default"]
        if (
            not isinstance(default_network, dict)
            or default_network.get("name") != expected_network
            or not default_network.get("external")
        ):
            errors.append(f"Resolved Compose default network must be external '{expected_network}'")

    _validate_named_volumes(data, errors)
    _validate_file_sources("secrets", data.get("secrets"), workspace_path, errors)
    _validate_file_sources("configs", data.get("configs"), workspace_path, errors)

    for service_name, service_config in services.items():
        if not isinstance(service_config, dict):
            errors.append(f"Service '{service_name}' must be a mapping")
            continue
        name = str(service_name)
        if service_config.get("privileged"):
            errors.append(f"Service '{name}': privileged is not allowed")
        for namespace_field in ("network_mode", "pid", "ipc", "uts", "userns_mode", "cgroup"):
            if service_config.get(namespace_field) is not None:
                errors.append(f"Service '{name}': {namespace_field} is not allowed")
        for capability_field in ("devices", "device_cgroup_rules", "cap_add"):
            if service_config.get(capability_field):
                errors.append(f"Service '{name}': {capability_field} is not allowed")
        for unsupported_field in ("volumes_from", "security_opt"):
            if service_config.get(unsupported_field):
                errors.append(f"Service '{name}': {unsupported_field} is not allowed")
        _validate_build(name, service_config, workspace_path, errors)
        _validate_volumes(name, service_config, errors, workspace_path=workspace_path, resolved=True)
        service_networks = service_config.get("networks")
        names = set(service_networks) if isinstance(service_networks, (dict, list)) else set()
        if names != {"default"}:
            errors.append(f"Service '{name}': only the worker default network is allowed")
        _validate_resource_limits(name, service_config, errors)

    return ValidationResult(valid=not errors, errors=errors)


def resolve_compose_path(compose_file: str, workspace_path: Path) -> tuple[Path, ValidationResult]:
    """Resolve a Compose path within the workspace, rejecting traversal."""
    try:
        resolved = (workspace_path / compose_file).resolve()
        resolved.relative_to(workspace_path.resolve())
    except ValueError:
        return workspace_path, ValidationResult(
            False, [f"Path traversal detected: '{compose_file}' resolves outside workspace"]
        )
    except (OSError, RuntimeError) as exc:
        return workspace_path, ValidationResult(False, [f"Failed to resolve path '{compose_file}': {exc}"])
    return resolved, ValidationResult(valid=True)
