"""Claude Code agent factory."""

from shared.schemas import ToolGroup, get_instructions_content
from workers_spawner.factories.base import AgentFactory
from workers_spawner.factories.registry import register_agent
from workers_spawner.models import AgentType


@register_agent(AgentType.CLAUDE_CODE)
class ClaudeCodeAgent(AgentFactory):
    """Factory for Anthropic Claude Code CLI agent."""

    def get_install_commands(self) -> list[str]:
        """Install Claude Code CLI via npm."""
        return [
            "npm install -g @anthropic-ai/claude-code",
        ]

    def get_agent_command(self) -> str:
        """Start Claude with unsafe permissions mode."""
        return "claude --dangerously-skip-permissions"

    def get_required_env_vars(self) -> list[str]:
        """Claude requires ANTHROPIC_API_KEY."""
        return ["ANTHROPIC_API_KEY"]

    def generate_instructions(self, allowed_tools: list[ToolGroup]) -> dict[str, str]:
        """Generate CLAUDE.md instruction file.

        Claude Code uses CLAUDE.md as its instruction file.
        """
        content = get_instructions_content(allowed_tools)
        return {"/workspace/CLAUDE.md": content}

    def get_setup_files(self) -> dict[str, str]:
        """Create Claude-specific config files.

        This can be used to set up ~/.claude/skills/ for Claude skills.
        """
        return {
            # Example: Claude skills could be added here
            # "/home/node/.claude/settings.json": json.dumps({...})
        }
