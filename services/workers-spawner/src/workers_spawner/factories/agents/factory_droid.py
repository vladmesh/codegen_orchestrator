"""Factory.ai Droid agent factory (stub)."""

from workers_spawner.factories.base import AgentFactory
from workers_spawner.factories.registry import register_agent
from workers_spawner.models import AgentType


@register_agent(AgentType.FACTORY_DROID)
class FactoryDroidAgent(AgentFactory):
    """Factory for Factory.ai Droid CLI agent.

    This is a stub implementation. Update when Factory.ai integration is needed.
    """

    def get_install_commands(self) -> list[str]:
        """Install Factory Droid CLI.

        TODO: Update with actual install command when available.
        """
        return [
            "# Factory Droid installation (placeholder)",
            "# pip install factory-droid-cli",
        ]

    def get_agent_command(self) -> str:
        """Start Factory Droid agent.

        TODO: Update with actual command when available.
        """
        return "factory-droid --interactive"

    def get_required_env_vars(self) -> list[str]:
        """Factory Droid required env vars.

        TODO: Update when API key requirements are known.
        """
        return ["FACTORY_API_KEY"]
