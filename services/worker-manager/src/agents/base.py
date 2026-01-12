from typing import Protocol, List


class AgentConfig(Protocol):
    """Protocol for defining agent-specific configuration."""

    def get_install_commands(self) -> List[str]:
        """Return a list of shell commands to install the agent in the Docker image."""
        ...

    def get_instruction_path(self) -> str:
        """Return the path to the instruction file (e.g., CLAUDE.md) inside the container."""
        ...

    def get_agent_command(self) -> str:
        """Return the command to start the agent."""
        ...
