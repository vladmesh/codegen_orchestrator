from typing import List
from .base import AgentConfig


class FactoryDroidAgent(AgentConfig):
    """Configuration for Factory.ai Droid agent."""

    def get_install_commands(self) -> List[str]:
        # Implementation assumes factory.ai installation script or procedure
        # For now, we simulate installing the CLI
        return [
            "curl -fsSL https://factory.ai/install.sh | sh",
        ]

    def get_instruction_path(self) -> str:
        return "/workspace/AGENTS.md"

    def get_agent_command(self) -> str:
        # factory.ai CLI
        return "droid"
