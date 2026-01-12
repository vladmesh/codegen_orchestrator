import os
from unittest.mock import patch

# Import will fail initially
from orchestrator_cli.permissions import PermissionManager, require_permission
import pytest
import typer


class TestPermissions:
    def test_allowed_tools_empty(self):
        """Empty env var allows all"""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": ""}):
            assert PermissionManager.is_tool_allowed("project")
            assert PermissionManager.is_tool_allowed("deploy")

    def test_allowed_tools_restrictive(self):
        """Specific tools allowed"""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project,deploy"}):
            assert PermissionManager.is_tool_allowed("project")
            assert PermissionManager.is_tool_allowed("deploy")
            assert not PermissionManager.is_tool_allowed("admin")

    def test_check_permission_raises(self):
        """check_permission raises Exit if not allowed"""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "deploy"}):
            with pytest.raises(typer.Exit):
                PermissionManager.check_permission("project")

    def test_decorator(self):
        """Decorator execution"""
        with patch.dict(os.environ, {"ORCHESTRATOR_ALLOWED_TOOLS": "project"}):

            @require_permission("project")
            def valid_func():
                return "ok"

            @require_permission("admin")
            def invalid_func():
                return "ok"

            assert valid_func() == "ok"

            with pytest.raises(typer.Exit):
                invalid_func()
