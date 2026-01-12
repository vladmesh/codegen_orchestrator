from collections.abc import Callable
from functools import wraps
import os

from rich.console import Console
import typer

console = Console()


class PermissionManager:
    """Manages tool permissions based on ORCHESTRATOR_ALLOWED_TOOLS env var."""

    @classmethod
    def get_allowed_tools(cls) -> list[str]:
        """Parse ORCHESTRATOR_ALLOWED_TOOLS env var."""
        env_value = os.getenv("ORCHESTRATOR_ALLOWED_TOOLS", "")
        if not env_value.strip():
            return []  # Empty = all allowed
        return [t.strip().lower() for t in env_value.split(",") if t.strip()]

    @classmethod
    def is_tool_allowed(cls, tool_name: str) -> bool:
        """Check if a tool group is allowed."""
        allowed = cls.get_allowed_tools()
        if not allowed:
            return True
        return tool_name.lower() in allowed

    @classmethod
    def check_permission(cls, tool_name: str) -> None:
        """Check permission and raise if denied."""
        if not cls.is_tool_allowed(tool_name):
            allowed = cls.get_allowed_tools()
            console.print(
                f"[bold red]Permission Denied:[/bold red] "
                f"Tool '{tool_name}' is not in allowed tools: {allowed}"
            )
            # Typer exit with error code
            raise typer.Exit(code=1)


def require_permission(tool_name: str) -> Callable:
    """Decorator to check permission before command execution."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            PermissionManager.check_permission(tool_name)
            return func(*args, **kwargs)

        return wrapper

    return decorator
