"""Factory.ai Droid agent factory."""

from shared.schemas import ToolGroup, get_instructions_content
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
        """Start Factory Droid in non-interactive mode.

        Note: 'droid exec' is required for automation/scripting.
        Plain 'droid' requires TTY (ink-based React CLI).
        Caller should append the prompt as an argument.
        """
        return "/home/worker/.local/bin/droid exec"

    def get_required_env_vars(self) -> list[str]:
        """Factory Droid requires FACTORY_API_KEY."""
        return ["FACTORY_API_KEY"]

    def generate_instructions(self, allowed_tools: list[ToolGroup]) -> dict[str, str]:
        """Generate AGENTS.md instruction file.

        Factory Droid and other agents use AGENTS.md as their instruction file.
        """
        content = get_instructions_content(allowed_tools)
        return {"/workspace/AGENTS.md": content}
