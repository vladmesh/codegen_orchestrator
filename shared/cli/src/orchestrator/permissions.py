"""Permission management for orchestrator CLI.

Checks ORCHESTRATOR_ALLOWED_TOOLS environment variable before command execution.
"""

from collections.abc import Callable
from functools import wraps
import os

from rich.console import Console
import typer

console = Console()


class PermissionManager:
    """Manages tool permissions based on ORCHESTRATOR_ALLOWED_TOOLS env var."""

    # Mapping from tool groups to command prefixes
    TOOL_MAPPING: dict[str, list[str]] = {
        "project": ["project"],
        "deploy": ["deploy"],
        "engineering": ["engineering"],
        "infra": ["infra"],
        "respond": ["respond"],
        "diagnose": ["diagnose"],
        "admin": ["admin"],
    }

    @classmethod
    def get_allowed_tools(cls) -> list[str]:
        """Parse ORCHESTRATOR_ALLOWED_TOOLS env var.

        Returns:
            List of allowed tool names. Empty list means all tools allowed.
        """
        env_value = os.getenv("ORCHESTRATOR_ALLOWED_TOOLS", "")
        if not env_value.strip():
            return []  # Empty = all allowed (permissive default for dev)
        return [t.strip().lower() for t in env_value.split(",") if t.strip()]

    @classmethod
    def is_tool_allowed(cls, tool_name: str) -> bool:
        """Check if a tool group is allowed.

        Args:
            tool_name: Tool group name (e.g., 'project', 'deploy')

        Returns:
            True if tool is allowed, False otherwise.
        """
        allowed = cls.get_allowed_tools()
        if not allowed:
            return True  # Empty list = all allowed
        return tool_name.lower() in allowed

    @classmethod
    def check_permission(cls, tool_name: str) -> None:
        """Check permission and raise if denied.

        Args:
            tool_name: Tool group name to check

        Raises:
            typer.Exit: If permission denied
        """
        if not cls.is_tool_allowed(tool_name):
            allowed = cls.get_allowed_tools()
            console.print(
                f"[bold red]Permission Denied:[/bold red] "
                f"Tool '{tool_name}' is not in allowed tools: {allowed}"
            )
            raise typer.Exit(code=1)


def require_permission(tool_name: str) -> Callable:
    """Decorator to check permission before command execution.

    Args:
        tool_name: Tool group name to check

    Returns:
        Decorator function
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            PermissionManager.check_permission(tool_name)
            return func(*args, **kwargs)

        return wrapper

    return decorator
