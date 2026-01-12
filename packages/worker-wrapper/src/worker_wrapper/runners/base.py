from typing import Protocol


class AgentRunner(Protocol):
    """Protocol for agent runners that execute commands in the container."""

    def build_command(self, prompt: str) -> list[str]:
        """Build the command to execute the agent."""
        ...
