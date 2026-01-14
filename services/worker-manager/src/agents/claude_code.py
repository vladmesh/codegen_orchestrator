from typing import List
from .base import AgentConfig


class ClaudeCodeAgent(AgentConfig):
    """Configuration for Anthropic's Claude Code agent.

    Claude CLI (Node.js + @anthropic-ai/claude-code) is pre-installed in
    worker-base-claude image for faster builds.
    """

    def get_install_commands(self) -> List[str]:
        # CLI is pre-installed in worker-base-claude image
        return []

    def get_instruction_path(self) -> str:
        return "/workspace/CLAUDE.md"

    def get_agent_command(self) -> str:
        # --dangerously-skip-permissions is required for autonomous execution
        return "claude --dangerously-skip-permissions"
