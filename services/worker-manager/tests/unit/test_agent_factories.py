from src.agents.claude_code import ClaudeCodeAgent
from src.agents.factory_droid import FactoryDroidAgent


class TestClaudeCodeAgent:
    def test_get_install_commands_returns_empty(self):
        """Install commands should be empty (CLI pre-installed in base image)."""
        agent = ClaudeCodeAgent()
        commands = agent.get_install_commands()
        assert commands == []

    def test_get_instruction_path_returns_claude_md(self):
        """Claude uses CLAUDE.md for instructions."""
        assert ClaudeCodeAgent().get_instruction_path() == "/workspace/CLAUDE.md"

    def test_get_agent_command_includes_dangerously_skip(self):
        """Agent command should skip permission prompts."""
        cmd = ClaudeCodeAgent().get_agent_command()
        assert "--dangerously-skip-permissions" in cmd


class TestFactoryDroidAgent:
    def test_get_install_commands_returns_empty(self):
        """Install commands should be empty (CLI pre-installed in base image)."""
        commands = FactoryDroidAgent().get_install_commands()
        assert commands == []

    def test_get_instruction_path_returns_agents_md(self):
        """Factory uses AGENTS.md for instructions."""
        assert FactoryDroidAgent().get_instruction_path() == "/workspace/AGENTS.md"
