"""Unit tests for PermissionManager."""

import os
from unittest.mock import patch

from orchestrator.permissions import PermissionManager, require_permission
import pytest
import typer


class TestPermissionManager:
    """Tests for PermissionManager class."""

    def test_get_allowed_tools_empty_env(self):
        """Empty env var returns empty list (all allowed)."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            assert PermissionManager.get_allowed_tools() == []

    def test_get_allowed_tools_not_set(self):
        """Missing env var returns empty list."""
        env = os.environ.copy()
        env.pop("ORCHESTRATOR_ALLOWED_TOOLS", None)
        with patch.dict(os.environ, env, clear=True):
            assert PermissionManager.get_allowed_tools() == []

    def test_get_allowed_tools_single(self):
        """Single tool is parsed correctly."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            assert PermissionManager.get_allowed_tools() == ["project"]

    def test_get_allowed_tools_multiple(self):
        """Multiple tools are parsed correctly."""
        with patch.dict(
            os.environ,
            {"ORCHESTRATOR_ALLOWED_TOOLS": "project, deploy, respond"},
            clear=False,
        ):
            result = PermissionManager.get_allowed_tools()
            assert result == ["project", "deploy", "respond"]

    def test_get_allowed_tools_strips_whitespace(self):
        """Whitespace is stripped from tool names."""
        with patch.dict(
            os.environ,
            {"ORCHESTRATOR_ALLOWED_TOOLS": "  project , deploy  "},
            clear=False,
        ):
            result = PermissionManager.get_allowed_tools()
            assert result == ["project", "deploy"]

    def test_get_allowed_tools_lowercase(self):
        """Tool names are lowercased."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "PROJECT,Deploy"}, clear=False):
            result = PermissionManager.get_allowed_tools()
            assert result == ["project", "deploy"]

    def test_is_tool_allowed_empty_allows_all(self):
        """Empty allowed list means all tools are allowed."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": ""}, clear=False):
            assert PermissionManager.is_tool_allowed("project") is True
            assert PermissionManager.is_tool_allowed("anything") is True

    def test_is_tool_allowed_specific_tools(self):
        """Only specified tools are allowed."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project,deploy"}, clear=False):
            assert PermissionManager.is_tool_allowed("project") is True
            assert PermissionManager.is_tool_allowed("deploy") is True
            assert PermissionManager.is_tool_allowed("admin") is False

    def test_is_tool_allowed_case_insensitive(self):
        """Tool check is case insensitive."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            assert PermissionManager.is_tool_allowed("PROJECT") is True
            assert PermissionManager.is_tool_allowed("Project") is True

    def test_check_permission_allowed(self):
        """check_permission passes for allowed tool."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            # Should not raise
            PermissionManager.check_permission("project")

    def test_check_permission_denied(self):
        """check_permission raises for denied tool."""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            with pytest.raises(typer.Exit) as exc_info:
                PermissionManager.check_permission("admin")
            assert exc_info.value.exit_code == 1


class TestRequirePermissionDecorator:
    """Tests for require_permission decorator."""

    def test_decorator_allows_permitted_tool(self):
        """Decorator allows execution for permitted tool."""

        @require_permission("project")
        def my_func():
            return "success"

        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            assert my_func() == "success"

    def test_decorator_blocks_denied_tool(self):
        """Decorator blocks execution for denied tool."""

        @require_permission("admin")
        def my_func():
            return "success"

        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            with pytest.raises(typer.Exit) as exc_info:
                my_func()
            assert exc_info.value.exit_code == 1

    def test_decorator_preserves_function_signature(self):
        """Decorator preserves the original function's behavior."""

        @require_permission("project")
        def greet(name: str) -> str:
            return f"Hello, {name}!"

        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}, clear=False):
            result = greet("World")
            assert result == "Hello, World!"
