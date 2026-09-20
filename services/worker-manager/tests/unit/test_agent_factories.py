from src.agents.claude_code import ClaudeCodeAgent
from src.agents.codex import CodexAgent
from src.agents.factory_droid import FactoryDroidAgent


class TestClaudeCodeAgent:
    def test_get_install_commands_returns_empty(self):
        """Install commands should be empty (CLI pre-installed in base image)."""
        agent = ClaudeCodeAgent()
        commands = agent.get_install_commands()
        assert commands == []

    def test_get_instruction_path_returns_claude_md(self):
        """Claude uses CLAUDE.md, which the product kit does not ship."""
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

    def test_instructions_never_overwrite_the_products_agents_md(self):
        """AGENTS.md is a tracked kit file; the orchestrator's instructions get their own."""
        assert FactoryDroidAgent().get_instruction_path() == "/workspace/WORKER_INSTRUCTIONS.md"


class TestCodexAgent:
    def test_uses_its_own_instruction_file_and_codex_exec(self):
        """Not the product's AGENTS.md: overwriting it put the orchestrator in the product."""
        agent = CodexAgent()
        assert agent.get_instruction_path() == "/workspace/WORKER_INSTRUCTIONS.md"
        assert agent.get_agent_command() == "codex exec"
