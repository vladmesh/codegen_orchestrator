"""Base abstract classes for agent and capability factories."""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from shared.schemas import ToolGroup

if TYPE_CHECKING:
    from workers_spawner.container_service import ContainerService


class AgentFactory(ABC):
    """Abstract factory for CLI agents.

    Each agent type (Claude Code, Factory Droid, etc.) has its own
    factory that knows how to install and configure that agent.
    """

    def __init__(self, container_service: "ContainerService"):
        """Initialize factory with container service dependency.

        Args:
            container_service: ContainerService instance for executing commands.
        """
        self.container_service = container_service

    @abstractmethod
    def get_install_commands(self) -> list[str]:
        """Get shell commands to install the agent.

        Returns:
            List of shell commands to run during container setup.
        """

    @abstractmethod
    def get_agent_command(self) -> str:
        """Get the command to start the agent.

        Returns:
            Command string to execute the agent CLI.
        """

    @abstractmethod
    def get_required_env_vars(self) -> list[str]:
        """Get list of required environment variable names.

        Returns:
            List of env var names that must be provided.
        """

    @abstractmethod
    def generate_instructions(self, allowed_tools: list[ToolGroup]) -> dict[str, str]:
        """Generate instruction files for this agent type.

        Args:
            allowed_tools: List of tool groups the agent is allowed to use.

        Returns:
            Dict of file path -> file content.
        """

    @abstractmethod
    async def send_message(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> dict:
        """Send text message to agent, get structured response.

        Args:
            agent_id: Container ID
            message: User message text
            session_context: Optional session state (agent-specific)

        Returns:
            {
                "response": str,  # Agent's response text
                "session_context": dict | None,  # Updated session state
                "metadata": dict  # Agent-specific metadata
            }
        """

    def get_optional_env_vars(self) -> dict[str, str]:
        """Get optional env vars with default values.

        Returns:
            Dict of env var name -> default value.
        """
        return {}

    def get_setup_files(self) -> dict[str, str]:
        """Get files to create during setup.

        Returns:
            Dict of file path -> file content.
        """
        return {}


class CapabilityFactory(ABC):
    """Abstract factory for agent capabilities.

    Capabilities are additional tools/packages that can be installed
    in the agent container (git, curl, node, python, etc.).
    """

    @abstractmethod
    def get_apt_packages(self) -> list[str]:
        """Get APT packages to install.

        Returns:
            List of package names for apt-get install.
        """

    def get_install_commands(self) -> list[str]:
        """Get additional install commands (npm, pip, etc.).

        Returns:
            List of shell commands to run after apt packages.
        """
        return []

    def get_env_vars(self) -> dict[str, str]:
        """Get environment variables to set.

        Returns:
            Dict of env var name -> value.
        """
        return {}
