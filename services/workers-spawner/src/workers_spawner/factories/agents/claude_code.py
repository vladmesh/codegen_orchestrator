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

    def get_persistent_command(self) -> str:
        """Get command for persistent interactive mode.

        Claude Code in persistent mode accepts input via stdin.
        """
        return "claude --dangerously-skip-permissions"

    def format_message_for_stdin(self, message: str) -> str:
        """Format message for stdin input.

        Claude expects messages followed by newline.
        """
        return f"{message}\n"

    async def send_message(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> dict:
        """Send message to Claude CLI and parse response."""
        import json

        session_id = session_context.get("session_id") if session_context else None

        # Escape single quotes for shell safety
        safe_message = message.replace("'", "'\\''")

        cmd_parts = [
            "claude",
            "--dangerously-skip-permissions",
            "-p",
            f"'{safe_message}'",
            "--output-format",
            "json",
        ]

        if session_id:
            cmd_parts.extend(["--resume", session_id])

        full_command = " ".join(cmd_parts)

        # Execute using injected container service
        result = await self.container_service.send_command(agent_id, full_command, timeout=120)

        # Parse response
        try:
            data = json.loads(result.output)
            response_text = data.get("result", "")
            new_session_id = data.get("session_id")

            return {
                "response": response_text,
                "session_context": {"session_id": new_session_id} if new_session_id else None,
                "metadata": {
                    "exit_code": result.exit_code,
                    "success": result.success,
                },
            }
        except json.JSONDecodeError:
            # Fallback for non-JSON output (errors etc)
            return {
                "response": result.output,
                "session_context": session_context,
                "metadata": {"parse_error": True, "success": result.success},
            }
