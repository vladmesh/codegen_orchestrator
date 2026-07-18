from dataclasses import dataclass

from .base import AgentRunner


@dataclass
class FactoryRunner(AgentRunner):
    """Runner for Factory Droid agent."""

    def build_command(self, prompt: str) -> list[str]:
        return [
            "droid",
            "exec",
            "--skip-permissions-unsafe",
            "--cwd",
            "/workspace",
            "-o",
            "json",
            prompt,
        ]
