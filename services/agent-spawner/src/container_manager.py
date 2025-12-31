"""Container lifecycle management for agent containers.

Simplified approach: use docker run for each execution, letting Docker
manage container lifecycle. Session persistence is handled via Claude's
--resume flag.
"""

import asyncio
import json
import os

import structlog

from .config import get_settings
from .models import ExecutionResult

logger = structlog.get_logger()


class ContainerManager:
    """Manages Docker containers for CLI agents.

    Uses docker run for each execution - containers are ephemeral.
    Session continuity is maintained via Claude's --resume flag.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    async def execute(
        self,
        user_id: str,
        prompt: str,
        session_id: str | None = None,
        timeout: int | None = None,
    ) -> ExecutionResult:
        """Execute a prompt in a new agent container.

        Args:
            user_id: User identifier (for container naming/tracking)
            prompt: Prompt to send to Claude
            session_id: Optional session ID for conversation continuation
            timeout: Execution timeout in seconds

        Returns:
            ExecutionResult with output and new session ID
        """
        timeout = timeout or self.settings.default_timeout_sec

        # Build docker run command
        cmd = [
            "docker",
            "run",
            "--rm",  # Remove container after execution
            f"--network={self.settings.container_network}",
            "--name",
            f"agent-{user_id}-{os.urandom(4).hex()}",  # Unique name
        ]

        # Add environment variables
        cmd.extend(["-e", f"ORCHESTRATOR_USER_ID={user_id}"])

        # Add Anthropic API key from environment if available
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            cmd.extend(["-e", f"ANTHROPIC_API_KEY={anthropic_key}"])

        # Mount Claude session directory for development
        # Docker needs HOST paths for volume mounts, not container paths
        if self.settings.host_claude_dir:
            cmd.extend(["-v", f"{self.settings.host_claude_dir}:/home/node/.claude"])

        # Add image
        cmd.append(self.settings.agent_image)

        # Claude CLI arguments
        cmd.extend(
            [
                "--dangerously-skip-permissions",
                "-p",
                prompt,
                "--output-format",
                "json",
            ]
        )

        if session_id:
            cmd.extend(["--resume", session_id])

        logger.info(
            "agent_executing",
            user_id=user_id,
            prompt_length=len(prompt),
            has_session=bool(session_id),
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return ExecutionResult(
                    success=False,
                    output="",
                    error=f"Execution timed out after {timeout}s",
                    exit_code=-1,
                )

            output = stdout.decode() if stdout else ""
            stderr_text = stderr.decode() if stderr else ""
            exit_code = proc.returncode or 0

            # Log stderr if present (often contains progress info)
            if stderr_text:
                logger.debug("agent_stderr", stderr=stderr_text[:500])

            # Try to parse JSON output for session ID
            new_session_id = session_id
            result_text = output

            try:
                parsed = json.loads(output)
                new_session_id = parsed.get("session_id", session_id)
                result_text = parsed.get("result", output)
            except json.JSONDecodeError:
                # Not JSON output, use raw
                pass

            success = exit_code == 0

            logger.info(
                "agent_execution_complete",
                user_id=user_id,
                success=success,
                exit_code=exit_code,
                output_length=len(result_text),
                has_new_session=new_session_id != session_id,
            )

            return ExecutionResult(
                success=success,
                output=result_text,
                session_id=new_session_id,
                exit_code=exit_code,
                error=stderr_text if not success else None,
            )

        except Exception as e:
            logger.error(
                "agent_execution_error",
                user_id=user_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return ExecutionResult(
                success=False,
                output="",
                error=str(e),
                exit_code=-1,
            )
