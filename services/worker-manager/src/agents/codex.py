from typing import List

from .base import AgentConfig


class CodexAgent(AgentConfig):
    """Configuration for the OpenAI Codex CLI developer worker."""

    def get_install_commands(self) -> List[str]:
        return []

    def get_instruction_path(self) -> str:
        return "/workspace/AGENTS.md"

    def get_agent_command(self) -> str:
        return "codex exec"
