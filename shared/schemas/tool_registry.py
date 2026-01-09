"""Tool registry for automatic command documentation.

Commands register themselves using the @register_tool decorator,
which enables automatic generation of CLAUDE.md documentation
based on allowed_tools configuration.
"""

from collections.abc import Callable

from .tool_groups import ToolGroup

# Global registry: group -> list of command info
TOOL_REGISTRY: dict[ToolGroup, list[dict]] = {
    ToolGroup.PROJECT: [],
    ToolGroup.DEPLOY: [],
    ToolGroup.ENGINEERING: [],
    ToolGroup.INFRA: [],
    ToolGroup.DIAGNOSE: [],
    ToolGroup.RESPOND: [],
}


def register_tool(group: ToolGroup):
    """Decorator to register a CLI command in a tool group.

    Usage:
        @app.command()
        @register_tool(ToolGroup.PROJECT)
        def create_project(...):
            '''Create a new project.'''
            ...

    The command's docstring becomes its description in documentation.
    """

    def decorator(func: Callable):
        # Extract command name from function name
        cmd_name = func.__name__.replace("_", "-")

        # Extract description from docstring
        doc = func.__doc__ or "No description"
        # Take first line of docstring
        description = doc.strip().split("\n")[0]

        # Register in the appropriate group
        TOOL_REGISTRY[group].append(
            {
                "name": cmd_name,
                "description": description,
                "function": func,
            }
        )

        return func

    return decorator


def get_registered_commands(group: ToolGroup) -> list[dict]:
    """Get all registered commands for a tool group.

    Returns:
        List of dicts with 'name', 'description', 'function' keys.
    """
    return TOOL_REGISTRY.get(group, [])


def clear_registry():
    """Clear all registered commands (for testing)."""
    for group in TOOL_REGISTRY:
        TOOL_REGISTRY[group].clear()


def load_cli_commands():
    """Import CLI command modules to trigger @register_tool decorators.

    This must be called before generating documentation to ensure
    all commands are registered in TOOL_REGISTRY.
    """
    from pathlib import Path
    import sys

    # Try to find shared/cli/src and add to path
    # This file is in shared/schemas/tool_registry.py
    # Path: shared/schemas/tool_registry.py -> parent -> parent = shared root
    try:
        current_file = Path(__file__)
        shared_root = current_file.parent.parent
        cli_src = shared_root / "cli" / "src"

        if cli_src.exists() and str(cli_src) not in sys.path:
            sys.path.append(str(cli_src))
    except Exception:  # noqa: S110
        # Ignore path errors
        pass

    try:
        # Import will trigger @register_tool decorators
        # pylint: disable=import-outside-toplevel,unused-import
        from orchestrator.commands import deploy, engineering, project  # noqa: F401
    except ImportError as e:
        # CLI not available (e.g., in other contexts)
        print(f"Warning: Failed to load CLI commands: {e}")
        pass
