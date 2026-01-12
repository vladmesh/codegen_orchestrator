"""Unit tests for ConfigParser."""

from unittest.mock import AsyncMock

import pytest
from workers_spawner.config_parser import ConfigParser
from workers_spawner.models import AgentType, CapabilityType, WorkerConfig

from shared.schemas import ToolGroup


@pytest.fixture
def mock_container_service():
    """Fixture for mock container service."""
    return AsyncMock()


class TestConfigParser:
    """Tests for ConfigParser class."""

    def test_basic_config(self, mock_container_service):
        """Parser works with minimal config."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=[ToolGroup.PROJECT, ToolGroup.RESPOND],
        )
        parser = ConfigParser(config, mock_container_service)

        assert parser.get_agent_command() == "claude --dangerously-skip-permissions"
        assert "ANTHROPIC_API_KEY" in parser.get_required_env_vars()

    def test_apt_packages_from_capabilities(self, mock_container_service):
        """APT packages are collected from capabilities."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.GIT, CapabilityType.CURL],
            allowed_tools=[ToolGroup.PROJECT],
        )
        parser = ConfigParser(config, mock_container_service)
        packages = parser.get_apt_packages()

        assert "git" in packages
        assert "curl" in packages
        # Packages should be deduplicated and sorted
        assert packages == sorted(set(packages))

    def test_install_commands_order(self, mock_container_service):
        """Install commands: capabilities first, then agent."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.GIT],
            allowed_tools=[ToolGroup.PROJECT],
        )
        parser = ConfigParser(config, mock_container_service)
        commands = parser.get_install_commands()

        # Git config should come before claude install
        git_idx = next(i for i, c in enumerate(commands) if "git config" in c)
        claude_idx = next(i for i, c in enumerate(commands) if "claude" in c)
        assert git_idx < claude_idx

    def test_env_vars_includes_allowed_tools(self, mock_container_service):
        """ORCHESTRATOR_ALLOWED_TOOLS is set from config."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=[ToolGroup.PROJECT, ToolGroup.DEPLOY, ToolGroup.RESPOND],
        )
        parser = ConfigParser(config, mock_container_service)
        env = parser.get_env_vars()

        assert "ORCHESTRATOR_ALLOWED_TOOLS" in env
        assert env["ORCHESTRATOR_ALLOWED_TOOLS"] == "project,deploy,respond"

    def test_env_vars_from_config_override(self, mock_container_service):
        """Config env_vars override capability defaults."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.NODE],
            allowed_tools=[ToolGroup.PROJECT],
            env_vars={"NPM_CONFIG_PREFIX": "/custom/path"},
        )
        parser = ConfigParser(config, mock_container_service)
        env = parser.get_env_vars()

        assert env["NPM_CONFIG_PREFIX"] == "/custom/path"

    def test_get_install_script(self, mock_container_service):
        """Install script is properly formatted bash."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.GIT],
            allowed_tools=[ToolGroup.PROJECT],
        )
        parser = ConfigParser(config, mock_container_service)
        script = parser.get_install_script()

        assert script.startswith("#!/bin/bash")
        assert "set -e" in script
        assert "apt-get install" in script

    def test_validate_missing_env_vars(self, mock_container_service):
        """Validation returns no errors - env vars are auto-injected at runtime.

        Note: Required env vars are NOT validated at config time because they
        can be auto-injected from spawner's environment at container creation.
        """
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=[ToolGroup.PROJECT],
            # No ANTHROPIC_API_KEY provided - but that's OK
        )
        parser = ConfigParser(config, mock_container_service)
        errors = parser.validate()

        # No validation errors for missing env vars (auto-injected at runtime)
        assert len(errors) == 0

    def test_validate_with_env_vars(self, mock_container_service):
        """Validation passes when required env vars provided."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=[ToolGroup.PROJECT],
            env_vars={"ANTHROPIC_API_KEY": "sk-xxx"},
        )
        parser = ConfigParser(config, mock_container_service)
        errors = parser.validate()

        assert len(errors) == 0

    def test_get_setup_files_includes_instructions(self, mock_container_service):
        """Setup files include generated instruction file."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=[ToolGroup.PROJECT, ToolGroup.DEPLOY],
        )
        parser = ConfigParser(config, mock_container_service)
        files = parser.get_setup_files()

        # Claude Code should generate CLAUDE.md
        assert "/workspace/CLAUDE.md" in files
        content = files["/workspace/CLAUDE.md"]

        # Content should include allowed tools documentation
        assert "orchestrator project" in content
        assert "orchestrator deploy" in content

        # Content should NOT include non-allowed tools
        assert "orchestrator engineering" not in content
