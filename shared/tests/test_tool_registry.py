"""Test tool registry and automatic command documentation."""

import builtins
import logging
from unittest.mock import patch

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
    assert commands[0]["name"] == "test-command"
    assert commands[0]["description"] == "Test command description."

    clear_registry()


def test_load_cli_commands_logs_import_error(caplog):
    """ImportError should be logged via logger, not print()."""
    real_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
        if name == "orchestrator_cli.commands":
            raise ImportError("test: no module")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with caplog.at_level(logging.WARNING):
            load_cli_commands()

    assert any("Failed to load CLI commands" in r.message for r in caplog.records)


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
