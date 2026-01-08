"""Claude Code agent factory."""

import json
import shlex
from typing import Any

import structlog

from shared.schemas import ToolGroup, get_instructions_content
from workers_spawner.factories.base import AgentFactory
from workers_spawner.factories.registry import register_agent
from workers_spawner.models import AgentType

logger = structlog.get_logger()


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

    async def send_message_headless(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> dict[str, Any]:
        """Send message using headless mode with clean JSON output.

        Uses claude -p with --output-format json for structured response.
        Session continuity via --resume session_id.

        Args:
            agent_id: Container ID
            message: User message text
            session_context: Optional session state (contains session_id)

        Returns:
            {
                "response": str,  # Agent's text response
                "session_context": dict,  # Updated session (session_id)
                "metadata": dict,  # Usage stats, model info
            }
        """
        session_id = session_context.get("session_id") if session_context else None

        # Build command with proper escaping
        # Use shlex.quote for safety
        cmd_parts = [
            "claude",
            "-p",
            shlex.quote(message),
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
        ]

        if session_id:
            cmd_parts.extend(["--resume", session_id])

        full_command = " ".join(cmd_parts)

        logger.info(
            "sending_headless_message",
            agent_id=agent_id,
            has_session=bool(session_id),
            message_length=len(message),
        )

        # Execute via ContainerService.send_command
        result = await self.container_service.send_command(agent_id, full_command, timeout=120)

        if result.exit_code != 0:
            logger.error(
                "headless_command_failed",
                agent_id=agent_id,
                exit_code=result.exit_code,
                error=result.error,
            )
            raise RuntimeError(f"Claude CLI failed: {result.error}")

        # Parse JSON response
        try:
            data = json.loads(result.output)

            return {
                "response": data["result"],
                "session_context": {"session_id": data["session_id"]},
                "metadata": {
                    "usage": data.get("usage", {}),
                    "model": data.get("model"),
                },
            }
        except json.JSONDecodeError as e:
            logger.error(
                "failed_to_parse_json",
                agent_id=agent_id,
                output_preview=result.output[:500],
                error=str(e),
            )
            raise RuntimeError(f"Failed to parse Claude response: {e}") from e
