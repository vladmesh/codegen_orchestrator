"""Test tool registry and automatic command documentation."""

from shared.schemas.tool_groups import get_instructions_content
from shared.schemas.tool_registry import (
    ToolGroup,
    clear_registry,
    get_registered_commands,
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
    assert commands[0]["name"] == "test-command"
    assert commands[0]["description"] == "Test command description."

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
