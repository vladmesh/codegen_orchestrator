"""Fail-closed policy checks for worker-scoped Docker Compose projects."""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ALLOWED_COMMANDS = {"up", "down", "build", "run", "ps", "logs", "stop"}
CONTAINER_CREATING_COMMANDS = {"up", "build", "run"}
BLOCKED_FLAGS = {"-it", "--interactive", "--tty", "-i", "-t"}
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

# Generated projects are allowed at most the same envelope as a capability worker.
# Compose accepts CPU values as decimal core counts and memory as bytes or an IEC/SI
# unit such as 512M or 1GiB.
MAX_CPU_LIMIT = 4.0
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


def _validate_volumes(service_name: str, service_config: dict[str, Any], errors: list[str]) -> None:
    volumes = service_config.get("volumes", [])
    if not isinstance(volumes, list):
        errors.append(f"Service '{service_name}': volumes must be a list")
        return
    for volume in volumes:
        source, target, volume_type = _volume_parts(volume)
        if volume_type == "bind" and source.startswith("/"):
            errors.append(f"Service '{service_name}': absolute bind mount source '{source}' is not allowed")
        if _is_socket_path(source) or _is_socket_path(target):
            errors.append(f"Service '{service_name}': Docker or Compose socket mount is not allowed")


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
    errors = []
    subcommand = None
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in VALUE_FLAGS:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        subcommand = arg
        break

    if subcommand is None:
        errors.append("No subcommand found in args")
    elif subcommand not in ALLOWED_COMMANDS:
        errors.append(f"Command '{subcommand}' is not allowed. Allowed: {sorted(ALLOWED_COMMANDS)}")

    for arg in args:
        if arg in BLOCKED_FLAGS:
            errors.append(f"Flag '{arg}' is not allowed (interactive flags are blocked)")
        if any(_flag_is_set(arg, flag) for flag in _SCOPE_OVERRIDE_FLAGS):
            errors.append(f"Flag '{arg}' cannot override the worker Compose scope")
        if arg.startswith("--file=") or (arg.startswith("-f") and arg != "-f"):
            errors.append(f"Flag '{arg}' must use a separate Compose file path")

    return ValidationResult(valid=not errors, errors=errors)


def validate_compose_file(content: str) -> ValidationResult:
    """Validate an individual selected Compose source before configuration resolution."""
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
    return ValidationResult(valid=not errors, errors=errors)


def validate_effective_compose(data: Any, worker_id: str) -> ValidationResult:
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

    for service_name, service_config in services.items():
        if not isinstance(service_config, dict):
            errors.append(f"Service '{service_name}' must be a mapping")
            continue
        name = str(service_name)
        if service_config.get("privileged"):
            errors.append(f"Service '{name}': privileged is not allowed")
        for namespace_field in ("network_mode", "pid", "ipc"):
            if service_config.get(namespace_field) == "host":
                errors.append(f"Service '{name}': {namespace_field}: host is not allowed")
        for capability_field in ("devices", "device_cgroup_rules", "cap_add"):
            if service_config.get(capability_field):
                errors.append(f"Service '{name}': {capability_field} is not allowed")
        _validate_volumes(name, service_config, errors)
        service_networks = service_config.get("networks")
        if service_networks is not None:
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
    except Exception as exc:
        return workspace_path, ValidationResult(False, [f"Failed to resolve path '{compose_file}': {exc}"])
    return resolved, ValidationResult(valid=True)
