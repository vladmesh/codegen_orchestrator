from dataclasses import dataclass, field
from pathlib import Path

import yaml

ALLOWED_COMMANDS = {"up", "down", "build", "run", "ps", "logs", "stop"}
BLOCKED_FLAGS = {"-it", "--interactive", "--tty", "-i", "-t"}


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def validate_command(args: list[str]) -> ValidationResult:
    """Validate a docker compose command argument list.

    Checks that the subcommand is in the whitelist and no blocked flags are used.
    """
    errors = []

    # Find first non-flag argument as subcommand
    subcommand = None
    for arg in args:
        if not arg.startswith("-"):
            subcommand = arg
            break

    if subcommand is None:
        errors.append("No subcommand found in args")
    elif subcommand not in ALLOWED_COMMANDS:
        errors.append(f"Command '{subcommand}' is not allowed. Allowed: {sorted(ALLOWED_COMMANDS)}")

    # Check for blocked flags
    for arg in args:
        if arg in BLOCKED_FLAGS:
            errors.append(f"Flag '{arg}' is not allowed (interactive flags are blocked)")

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def validate_compose_file(content: str) -> ValidationResult:
    """Validate docker-compose YAML content.

    Blocks: absolute volume mounts, ports directive.
    """
    errors = []

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return ValidationResult(valid=False, errors=[f"Invalid YAML: {e}"])

    if not isinstance(data, dict):
        return ValidationResult(valid=False, errors=["Compose file must be a YAML mapping"])

    services = data.get("services", {})
    if not isinstance(services, dict):
        return ValidationResult(valid=False, errors=["'services' must be a mapping"])

    for service_name, service_config in services.items():
        if not isinstance(service_config, dict):
            continue

        # Block ports directive
        if "ports" in service_config:
            errors.append(
                f"Service '{service_name}': 'ports' directive is not allowed "
                "(use service names for inter-service communication)"
            )

        # Block absolute volume mounts
        volumes = service_config.get("volumes", [])
        for vol in volumes:
            if isinstance(vol, str):
                # Short syntax: "host:container" or "named:container" or just "container"
                parts = vol.split(":")
                host_part = parts[0] if len(parts) > 1 else None
                if host_part and host_part.startswith("/"):
                    errors.append(f"Service '{service_name}': absolute volume mount '{vol}' is not allowed")
            elif isinstance(vol, dict):
                # Long syntax
                source = vol.get("source", "")
                vol_type = vol.get("type", "volume")
                if vol_type == "bind" and source.startswith("/"):
                    errors.append(f"Service '{service_name}': absolute bind mount source '{source}' is not allowed")

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def resolve_compose_path(compose_file: str, workspace_path: Path) -> tuple[Path, ValidationResult]:
    """Resolve compose file path within workspace, checking for path traversal.

    Returns (resolved_path, validation_result).
    """
    errors = []

    try:
        resolved = (workspace_path / compose_file).resolve()
        workspace_resolved = workspace_path.resolve()

        # Check path traversal: resolved must be under workspace
        resolved.relative_to(workspace_resolved)
    except ValueError:
        errors.append(f"Path traversal detected: '{compose_file}' resolves outside workspace")
        return workspace_path, ValidationResult(valid=False, errors=errors)
    except Exception as e:
        errors.append(f"Failed to resolve path '{compose_file}': {e}")
        return workspace_path, ValidationResult(valid=False, errors=errors)

    return resolved, ValidationResult(valid=True)
