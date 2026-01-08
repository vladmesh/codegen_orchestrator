"""Factory.ai Droid agent factory."""

import json
import shlex
from typing import Any

import structlog

from shared.schemas import ToolGroup, get_instructions_content
from workers_spawner.factories.base import AgentFactory
from workers_spawner.factories.registry import register_agent
from workers_spawner.models import AgentType

logger = structlog.get_logger()


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

    async def send_message_headless(
        self,
        agent_id: str,
        message: str,
        session_context: dict | None = None,
    ) -> dict[str, Any]:
        """Send message to Factory Droid using headless exec mode.

        Note: Factory Droid has different session management.
        Context is handled via workspace state, not session_id.

        Args:
            agent_id: Container ID
            message: User message text
            session_context: Optional session state (preserved as-is)

        Returns:
            {
                "response": str,  # Agent's text response
                "session_context": dict | None,  # Preserved session state
                "metadata": dict,  # Agent-specific metadata
            }
        """
        cmd = f"/home/worker/.local/bin/droid exec -o json {shlex.quote(message)}"

        logger.info(
            "sending_headless_message_droid",
            agent_id=agent_id,
            message_length=len(message),
        )

        result = await self.container_service.send_command(agent_id, cmd, timeout=120)

        if result.exit_code != 0:
            logger.error(
                "droid_exec_failed",
                agent_id=agent_id,
                exit_code=result.exit_code,
                error=result.error,
            )
            raise RuntimeError(f"Droid exec failed: {result.error}")

        # Parse output (droid exec format may vary)
        try:
            data = json.loads(result.output)
            return {
                "response": data.get("result", result.output),
                "session_context": session_context,  # Preserve as-is
                "metadata": {},
            }
        except json.JSONDecodeError:
            # Fallback: treat output as plain text
            logger.warning(
                "droid_output_not_json",
                agent_id=agent_id,
                output_preview=result.output[:500],
            )
            return {"response": result.output, "session_context": session_context, "metadata": {}}
