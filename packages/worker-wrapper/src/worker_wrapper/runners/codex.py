from dataclasses import dataclass

from .base import AgentRunner


@dataclass
class CodexRunner(AgentRunner):
    """Runner for Codex CLI non-interactive developer work."""

    allow_non_git_workspace: bool = False

    def build_command(self, prompt: str) -> list[str]:
        command = [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "--config",
            "sandbox_workspace_write.network_access=true",
        ]
        # A central QA worker intentionally receives an empty scratch directory,
        # not a checkout. This is Codex's native opt-in for that execution mode;
        # developer workers retain the usual repository check.
        if self.allow_non_git_workspace:
            command.append("--skip-git-repo-check")
        return [*command, prompt]
