"""Factory.ai Droid agent factory."""

from workers_spawner.factories.base import AgentFactory
from workers_spawner.factories.registry import register_agent
from workers_spawner.models import AgentType


@register_agent(AgentType.FACTORY_DROID)
class FactoryDroidAgent(AgentFactory):
    """Factory for Factory.ai Droid CLI agent."""

    def get_install_commands(self) -> list[str]:
        """Install Factory Droid CLI from official installer."""
        return [
            "curl -fsSL https://app.factory.ai/cli | sh",
        ]

    def get_agent_command(self) -> str:
        """Start Factory Droid in interactive mode.

        Note: Droid is installed to ~/.local/bin by default.
        """
        return "/home/worker/.local/bin/droid"

    def get_required_env_vars(self) -> list[str]:
        """Factory Droid requires FACTORY_API_KEY."""
        return ["FACTORY_API_KEY"]
