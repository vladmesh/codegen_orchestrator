from dataclasses import dataclass

from .base import AgentRunner


@dataclass
class CodexRunner(AgentRunner):
    """Runner for Codex CLI non-interactive developer work."""

    def build_command(self, prompt: str) -> list[str]:
        return ["codex", "exec", "--sandbox", "workspace-write", prompt]
