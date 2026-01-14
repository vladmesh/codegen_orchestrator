from typing import List
from .base import AgentConfig


class FactoryDroidAgent(AgentConfig):
    """Configuration for Factory.ai Droid agent.

    Factory CLI (droid) is pre-installed in worker-base-factory image
    for faster builds.
    """

    def get_install_commands(self) -> List[str]:
        # CLI is pre-installed in worker-base-factory image
        return []

    def get_instruction_path(self) -> str:
        return "/workspace/AGENTS.md"

    def get_agent_command(self) -> str:
        # factory.ai CLI
        return "droid"
