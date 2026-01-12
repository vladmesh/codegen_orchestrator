from typing import List
from .base import AgentConfig


class ClaudeCodeAgent(AgentConfig):
    """Configuration for Anthropic's Claude Code agent."""

    def get_install_commands(self) -> List[str]:
        return [
            # Node.js is pre-installed in worker-base, but we ensure npm is available
            "npm install -g @anthropic-ai/claude-code",
        ]

    def get_instruction_path(self) -> str:
        return "/workspace/CLAUDE.md"

    def get_agent_command(self) -> str:
        # --dangerously-skip-permissions is required for autonomous execution
        return "claude --dangerously-skip-permissions"
