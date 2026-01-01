"""Unit tests for ConfigParser."""

from workers_spawner.config_parser import ConfigParser
from workers_spawner.models import AgentType, CapabilityType, WorkerConfig


class TestConfigParser:
    """Tests for ConfigParser class."""

    def test_basic_config(self):
        """Parser works with minimal config."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=["project", "respond"],
        )
        parser = ConfigParser(config)

        assert parser.get_agent_command() == "claude --dangerously-skip-permissions"
        assert "ANTHROPIC_API_KEY" in parser.get_required_env_vars()

    def test_apt_packages_from_capabilities(self):
        """APT packages are collected from capabilities."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.GIT, CapabilityType.CURL],
            allowed_tools=["project"],
        )
        parser = ConfigParser(config)
        packages = parser.get_apt_packages()

        assert "git" in packages
        assert "curl" in packages
        # Packages should be deduplicated and sorted
        assert packages == sorted(set(packages))

    def test_install_commands_order(self):
        """Install commands: capabilities first, then agent."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.GIT],
            allowed_tools=["project"],
        )
        parser = ConfigParser(config)
        commands = parser.get_install_commands()

        # Git config should come before claude install
        git_idx = next(i for i, c in enumerate(commands) if "git config" in c)
        claude_idx = next(i for i, c in enumerate(commands) if "claude" in c)
        assert git_idx < claude_idx

    def test_env_vars_includes_allowed_tools(self):
        """ORCHESTRATOR_ALLOWED_TOOLS is set from config."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=["project", "deploy", "respond"],
        )
        parser = ConfigParser(config)
        env = parser.get_env_vars()

        assert "ORCHESTRATOR_ALLOWED_TOOLS" in env
        assert env["ORCHESTRATOR_ALLOWED_TOOLS"] == "project,deploy,respond"

    def test_env_vars_from_config_override(self):
        """Config env_vars override capability defaults."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.NODE],
            allowed_tools=["project"],
            env_vars={"NPM_CONFIG_PREFIX": "/custom/path"},
        )
        parser = ConfigParser(config)
        env = parser.get_env_vars()

        assert env["NPM_CONFIG_PREFIX"] == "/custom/path"

    def test_get_install_script(self):
        """Install script is properly formatted bash."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            capabilities=[CapabilityType.GIT],
            allowed_tools=["project"],
        )
        parser = ConfigParser(config)
        script = parser.get_install_script()

        assert script.startswith("#!/bin/bash")
        assert "set -e" in script
        assert "apt-get install" in script

    def test_validate_missing_env_vars(self):
        """Validation returns no errors - env vars are auto-injected at runtime.

        Note: Required env vars are NOT validated at config time because they
        can be auto-injected from spawner's environment at container creation.
        """
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=["project"],
            # No ANTHROPIC_API_KEY provided - but that's OK
        )
        parser = ConfigParser(config)
        errors = parser.validate()

        # No validation errors for missing env vars (auto-injected at runtime)
        assert len(errors) == 0

    def test_validate_with_env_vars(self):
        """Validation passes when required env vars provided."""
        config = WorkerConfig(
            name="Test Agent",
            agent=AgentType.CLAUDE_CODE,
            allowed_tools=["project"],
            env_vars={"ANTHROPIC_API_KEY": "sk-xxx"},
        )
        parser = ConfigParser(config)
        errors = parser.validate()

        assert len(errors) == 0
