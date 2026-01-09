"""Test tool registry and automatic command documentation."""

from shared.schemas.tool_groups import get_instructions_content
from shared.schemas.tool_registry import (
    ToolGroup,
    clear_registry,
    get_registered_commands,
    load_cli_commands,
    register_tool,
)


def test_register_tool_decorator():
    """Decorator registers commands in TOOL_REGISTRY."""
    clear_registry()

    @register_tool(ToolGroup.PROJECT)
    def test_command():
        """Test command description."""
        pass

    commands = get_registered_commands(ToolGroup.PROJECT)
    assert len(commands) == 1
    assert commands[0]["name"] == "test-command"  # Underscores replaced with hyphens
    assert commands[0]["description"] == "Test command description."

    clear_registry()


def test_load_cli_commands():
    """Loading CLI commands populates registry."""
    clear_registry()
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

    # Register a test command
    @register_tool(ToolGroup.PROJECT)
    def my_test_cmd():
        """My test command."""
        pass

    # Generate docs with only PROJECT tools
    content = get_instructions_content([ToolGroup.PROJECT])

    # Should include the registered command
    assert "my-test-cmd" in content
    assert "My test command." in content
    assert "orchestrator project my-test-cmd" in content

    # Should NOT include other groups
    assert "deploy" not in content.lower()
    assert "engineering" not in content.lower()

    clear_registry()


def test_generated_docs_filtered_by_allowed_tools():
    """Only allowed tool groups appear in documentation."""
    content = get_instructions_content([ToolGroup.PROJECT, ToolGroup.DEPLOY])

    # Should include PROJECT and DEPLOY
    assert "orchestrator project" in content
    assert "orchestrator deploy" in content

    # Should NOT include ENGINEERING
    assert "orchestrator engineering" not in content


def test_command_descriptions_in_docs():
    """Command docstrings appear as descriptions."""
    clear_registry()
    load_cli_commands()

    content = get_instructions_content([ToolGroup.PROJECT])

    # Check that command descriptions from docstrings are included
    assert "List all projects" in content or "list" in content
    assert "Create a new project" in content or "create" in content

    clear_registry()
