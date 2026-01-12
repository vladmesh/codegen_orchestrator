from dataclasses import dataclass

from .base import AgentRunner


@dataclass
class FactoryRunner(AgentRunner):
    """Runner for Factory Droid agent."""

    def build_command(self, prompt: str) -> list[str]:
        # factory.ai is the CLI tool name, command is droid exec
        # Based on specs: droid exec -o json "prompt"
        return ["droid", "exec", "-o", "json", prompt]
