"""Test tool registry and automatic command documentation.

These tests require orchestrator_cli to be installed (for load_cli_commands).
They will be skipped in environments without it (e.g., api-test-runner).
"""

import pytest

from shared.schemas.tool_groups import get_instructions_content
from shared.schemas.tool_registry import (
    ToolGroup,
    clear_registry,
    get_registered_commands,
    load_cli_commands,
    register_tool,
)

try:
    import orchestrator_cli  # noqa: F401

    HAS_CLI = True
except ImportError:
    HAS_CLI = False

requires_cli = pytest.mark.skipif(not HAS_CLI, reason="orchestrator_cli not installed")


def test_register_tool_decorator():
    """Decorator registers commands in TOOL_REGISTRY."""
    clear_registry()

    @register_tool(ToolGroup.PROJECT)
    def test_command():
        """Test command description."""
        pass

    commands = get_registered_commands(ToolGroup.PROJECT)
    assert len(commands) == 1
    assert commands[0]["name"] == "test-command"
    assert commands[0]["description"] == "Test command description."

    clear_registry()


@requires_cli
def test_load_cli_commands():
    """Loading CLI commands populates registry."""
    clear_registry()

    import sys

    modules_to_clear = [m for m in sys.modules if m.startswith("orchestrator_cli.commands")]
    for module_name in modules_to_clear:
        del sys.modules[module_name]

    load_cli_commands()

    # Check PROJECT commands
    project_commands = get_registered_commands(ToolGroup.PROJECT)
    command_names = [cmd["name"] for cmd in project_commands]

    assert "list" in command_names
    assert "get" in command_names
    assert "create" in command_names
    assert "set-secret" in command_names

    # Check DEPLOY commands
    deploy_commands = get_registered_commands(ToolGroup.DEPLOY)
    deploy_names = [cmd["name"] for cmd in deploy_commands]

    assert "trigger" in deploy_names
    assert "status" in deploy_names

    # Check ENGINEERING commands
    eng_commands = get_registered_commands(ToolGroup.ENGINEERING)
    eng_names = [cmd["name"] for cmd in eng_commands]

    assert "trigger" in eng_names
    assert "status" in eng_names

    clear_registry()


def test_get_instructions_content_uses_registry():
    """Documentation generation uses registered commands."""
    clear_registry()

    @register_tool(ToolGroup.PROJECT)
    def my_test_cmd():
        """My test command."""
        pass

    content = get_instructions_content([ToolGroup.PROJECT])

    assert "my-test-cmd" in content
    assert "My test command." in content
    assert "orchestrator project my-test-cmd" in content

    assert "## Deploy Commands" not in content
    assert "## Engineering Commands" not in content

    clear_registry()


@requires_cli
def test_generated_docs_filtered_by_allowed_tools():
    """Only allowed tool groups appear in documentation."""
    clear_registry()
    load_cli_commands()

    content = get_instructions_content([ToolGroup.PROJECT, ToolGroup.DEPLOY])

    assert "## Project Commands" in content
    assert "## Deploy Commands" in content
    assert "## Engineering Commands" not in content

    clear_registry()


@requires_cli
def test_command_descriptions_in_docs():
    """Command docstrings appear as descriptions."""
    clear_registry()
    load_cli_commands()

    content = get_instructions_content([ToolGroup.PROJECT])

    assert "List all projects" in content or "list" in content
    assert "Create a new project" in content or "create" in content

    clear_registry()
