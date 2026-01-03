"""Unit tests for agent and capability factories."""

import pytest
from workers_spawner.factories import (
    AgentFactory,
    CapabilityFactory,
    get_agent_factory,
    get_capability_factory,
)
from workers_spawner.factories.agents.claude_code import ClaudeCodeAgent
from workers_spawner.factories.agents.factory_droid import FactoryDroidAgent
from workers_spawner.factories.capabilities.curl import CurlCapability
from workers_spawner.factories.capabilities.git import GitCapability
from workers_spawner.factories.capabilities.node import NodeCapability
from workers_spawner.factories.capabilities.python import PythonCapability
from workers_spawner.models import AgentType, CapabilityType

from shared.schemas import ToolGroup


class TestAgentRegistry:
    """Tests for agent factory registry."""

    def test_get_claude_code_factory(self):
        """Claude Code factory is registered."""
        factory = get_agent_factory(AgentType.CLAUDE_CODE)
        assert isinstance(factory, ClaudeCodeAgent)
        assert isinstance(factory, AgentFactory)

    def test_get_factory_droid_factory(self):
        """Factory Droid factory is registered."""
        factory = get_agent_factory(AgentType.FACTORY_DROID)
        assert isinstance(factory, FactoryDroidAgent)

    def test_unknown_agent_raises(self):
        """Unknown agent type raises ValueError."""
        with pytest.raises(ValueError, match="Unknown agent type"):
            get_agent_factory(AgentType.CODEX)  # type: ignore


class TestCapabilityRegistry:
    """Tests for capability factory registry."""

    def test_get_git_capability(self):
        """Git capability is registered."""
        factory = get_capability_factory(CapabilityType.GIT)
        assert isinstance(factory, GitCapability)
        assert isinstance(factory, CapabilityFactory)

    def test_get_curl_capability(self):
        """Curl capability is registered."""
        factory = get_capability_factory(CapabilityType.CURL)
        assert isinstance(factory, CurlCapability)

    def test_get_node_capability(self):
        """Node capability is registered."""
        factory = get_capability_factory(CapabilityType.NODE)
        assert isinstance(factory, NodeCapability)

    def test_get_python_capability(self):
        """Python capability is registered."""
        factory = get_capability_factory(CapabilityType.PYTHON)
        assert isinstance(factory, PythonCapability)

    def test_get_docker_capability(self):
        """Docker capability is registered."""
        from workers_spawner.factories.capabilities.docker import DockerCapability

        factory = get_capability_factory(CapabilityType.DOCKER)
        assert isinstance(factory, DockerCapability)


class TestClaudeCodeAgent:
    """Tests for Claude Code agent factory."""

    def test_install_commands(self):
        """Install commands include npm install."""
        factory = ClaudeCodeAgent()
        commands = factory.get_install_commands()
        assert len(commands) >= 1
        assert any("claude-code" in cmd for cmd in commands)

    def test_agent_command(self):
        """Agent command includes dangerously-skip-permissions."""
        factory = ClaudeCodeAgent()
        cmd = factory.get_agent_command()
        assert "claude" in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_required_env_vars(self):
        """Requires ANTHROPIC_API_KEY."""
        factory = ClaudeCodeAgent()
        required = factory.get_required_env_vars()
        assert "ANTHROPIC_API_KEY" in required

    def test_generate_instructions_returns_claude_md(self):
        """Generate instructions returns CLAUDE.md file."""
        factory = ClaudeCodeAgent()
        files = factory.generate_instructions([ToolGroup.PROJECT, ToolGroup.DEPLOY])

        assert "/workspace/CLAUDE.md" in files
        content = files["/workspace/CLAUDE.md"]
        assert "orchestrator project" in content.lower()
        assert "orchestrator deploy" in content.lower()

    def test_generate_instructions_filters_tools(self):
        """Only allowed tools are included in instructions."""
        factory = ClaudeCodeAgent()
        files = factory.generate_instructions([ToolGroup.PROJECT])

        content = files["/workspace/CLAUDE.md"]
        assert "orchestrator project" in content.lower()
        # Deploy should NOT be included
        assert "deploy trigger" not in content.lower()


class TestFactoryDroidAgent:
    """Tests for Factory Droid agent factory."""

    def test_generate_instructions_returns_agents_md(self):
        """Generate instructions returns AGENTS.md file."""
        factory = FactoryDroidAgent()
        files = factory.generate_instructions([ToolGroup.PROJECT, ToolGroup.DEPLOY])

        assert "/workspace/AGENTS.md" in files
        content = files["/workspace/AGENTS.md"]
        assert "orchestrator project" in content.lower()
        assert "orchestrator deploy" in content.lower()


class TestGitCapability:
    """Tests for Git capability factory."""

    def test_apt_packages(self):
        """Returns git package."""
        factory = GitCapability()
        packages = factory.get_apt_packages()
        assert "git" in packages

    def test_install_commands(self):
        """Configures git user."""
        factory = GitCapability()
        commands = factory.get_install_commands()
        assert any("git config" in cmd for cmd in commands)


class TestNodeCapability:
    """Tests for Node capability factory."""

    def test_install_commands(self):
        """Sets up npm-global directory (Node.js is pre-installed in base image)."""
        factory = NodeCapability()
        commands = factory.get_install_commands()
        assert any("npm-global" in cmd for cmd in commands)

    def test_env_vars(self):
        """Sets NPM config prefix."""
        factory = NodeCapability()
        env = factory.get_env_vars()
        assert "NPM_CONFIG_PREFIX" in env
