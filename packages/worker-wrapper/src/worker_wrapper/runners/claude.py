from dataclasses import dataclass

from .base import AgentRunner


@dataclass
class ClaudeRunner(AgentRunner):
    """Runner for Claude Code agent."""

    session_id: str | None = None

    def build_command(self, prompt: str) -> list[str]:
        cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt, "--output-format", "json"]
        if self.session_id:
            cmd.extend(["--resume", self.session_id])
        return cmd
