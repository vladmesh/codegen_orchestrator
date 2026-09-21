from shared.constants import WorkerWorkspace

from .base import AgentConfig


class CodexAgent(AgentConfig):
    """Configuration for the OpenAI Codex CLI developer worker."""

    def get_install_commands(self) -> list[str]:
        return []

    def get_instruction_path(self) -> str:
        # Not AGENTS.md: in a scaffolded product that is a *tracked* kit file, and
        # overwriting it put orchestrator instructions into the product's history.
        # Codex reads the product's own AGENTS.md by itself; the turn prompt names this
        # file, and the wrapper keeps it out of the product's commits.
        return f"/workspace/{WorkerWorkspace.AGENT_INSTRUCTIONS}"

    def get_agent_command(self) -> str:
        return "codex exec"
