from src.agents.claude_code import ClaudeCodeAgent
from src.agents.factory_droid import FactoryDroidAgent


class TestClaudeCodeAgent:
    def test_get_install_commands_includes_npm_install(self):
        """Install commands should include Claude Code npm install."""
        agent = ClaudeCodeAgent()
        commands = agent.get_install_commands()
        assert any("npm install" in cmd and "claude-code" in cmd for cmd in commands)

    def test_get_instruction_path_returns_claude_md(self):
        """Claude uses CLAUDE.md for instructions."""
        assert ClaudeCodeAgent().get_instruction_path() == "/workspace/CLAUDE.md"

    def test_get_agent_command_includes_dangerously_skip(self):
        """Agent command should skip permission prompts."""
        cmd = ClaudeCodeAgent().get_agent_command()
        assert "--dangerously-skip-permissions" in cmd


class TestFactoryDroidAgent:
    def test_get_install_commands_includes_curl_install(self):
        """Install commands should include Factory CLI install."""
        commands = FactoryDroidAgent().get_install_commands()
        assert any("factory.ai" in cmd for cmd in commands)

    def test_get_instruction_path_returns_agents_md(self):
        """Factory uses AGENTS.md for instructions."""
        assert FactoryDroidAgent().get_instruction_path() == "/workspace/AGENTS.md"
