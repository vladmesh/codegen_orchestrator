from shared.constants import WorkerWorkspace

from .base import AgentConfig


class ClaudeCodeAgent(AgentConfig):
    """Configuration for Anthropic's Claude Code agent.

    Claude CLI (Node.js + @anthropic-ai/claude-code) is pre-installed in
    worker-base-claude image for faster builds.
    """

    def get_install_commands(self) -> list[str]:
        # CLI is pre-installed in worker-base-claude image
        return []

    def get_instruction_path(self) -> str:
        # The product kit ships no CLAUDE.md, so this file is the orchestrator's
        # alone. The wrapper keeps it out of the product's commits.
        return f"/workspace/{WorkerWorkspace.CLAUDE_INSTRUCTIONS}"

    def get_agent_command(self) -> str:
        # --dangerously-skip-permissions is required for autonomous execution
        return "claude --dangerously-skip-permissions"
