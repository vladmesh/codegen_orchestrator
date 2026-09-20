from shared.constants import WorkerWorkspace

from .base import AgentConfig


class FactoryDroidAgent(AgentConfig):
    """Configuration for Factory.ai Droid agent.

    Factory CLI (droid) is pre-installed in worker-base-factory image
    for faster builds.
    """

    def get_install_commands(self) -> list[str]:
        # CLI is pre-installed in worker-base-factory image
        return []

    def get_instruction_path(self) -> str:
        # Not AGENTS.md: in a scaffolded product that is a *tracked* kit file, and
        # overwriting it put orchestrator instructions into the product's history.
        # Droid reads the product's own AGENTS.md by itself; the turn prompt names this
        # file, and the wrapper keeps it out of the product's commits.
        return f"/workspace/{WorkerWorkspace.AGENT_INSTRUCTIONS}"

    def get_agent_command(self) -> str:
        # factory.ai CLI
        return "droid"
