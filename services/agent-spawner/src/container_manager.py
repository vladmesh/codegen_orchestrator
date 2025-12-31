"""Container lifecycle management for agent containers."""

import asyncio
import json
import os

import structlog

from .config import get_settings
from .models import ContainerStatus, ExecutionResult

logger = structlog.get_logger()


class ContainerManager:
    """Manages Docker containers for CLI agents."""

    def __init__(self) -> None:
        self.settings = get_settings()

    async def create_container(
        self,
        user_id: str,
        api_token: str | None = None,
    ) -> str:
        """Create a new agent container for user.

        Args:
            user_id: User identifier
            api_token: Optional API token for the orchestrator

        Returns:
            Container ID
        """
        # Build docker create command (not run - we manage lifecycle separately)
        cmd = [
            "docker",
            "create",
            f"--name=agent-{user_id}",
            f"--network={self.settings.container_network}",
            "--restart=no",
            "-e",
            f"ORCHESTRATOR_USER_ID={user_id}",
        ]

        # Add API token if provided
        if api_token:
            cmd.extend(["-e", f"ORCHESTRATOR_API_TOKEN={api_token}"])

        # Add Anthropic API key from environment if available
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            cmd.extend(["-e", f"ANTHROPIC_API_KEY={anthropic_key}"])

        # For development: mount Claude session directory
        claude_dir = os.path.expanduser("~/.claude")
        if os.path.exists(claude_dir):
            cmd.extend(["-v", f"{claude_dir}:/home/agent/.claude:ro"])

        cmd.append(self.settings.agent_image)

        logger.info(
            "container_creating",
            user_id=user_id,
            image=self.settings.agent_image,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode() if stderr else "Unknown error"
                logger.error(
                    "container_create_failed",
                    user_id=user_id,
                    error=error_msg,
                    exit_code=proc.returncode,
                )
                raise RuntimeError(f"Failed to create container: {error_msg}")

            container_id = stdout.decode().strip()
            logger.info(
                "container_created",
                user_id=user_id,
                container_id=container_id[:12],
            )
            return container_id

        except Exception as e:
            logger.error(
                "container_create_error",
                user_id=user_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    async def start_container(self, container_id: str) -> None:
        """Start a stopped container."""
        await self._docker_command("start", container_id)
        logger.info("container_started", container_id=container_id[:12])

    async def pause_container(self, container_id: str) -> None:
        """Pause a running container."""
        await self._docker_command("pause", container_id)
        logger.info("container_paused", container_id=container_id[:12])

    async def resume_container(self, container_id: str) -> None:
        """Resume a paused container."""
        await self._docker_command("unpause", container_id)
        logger.info("container_resumed", container_id=container_id[:12])

    async def destroy_container(self, container_id: str) -> None:
        """Remove a container."""
        # Force remove (handles running containers)
        await self._docker_command("rm", "-f", container_id)
        logger.info("container_destroyed", container_id=container_id[:12])

    async def get_container_status(self, container_id: str) -> ContainerStatus | None:
        """Get container status."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}}",
                container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                return None

            status = stdout.decode().strip()
            status_map = {
                "created": ContainerStatus.CREATING,
                "running": ContainerStatus.RUNNING,
                "paused": ContainerStatus.PAUSED,
                "exited": ContainerStatus.DESTROYED,
                "dead": ContainerStatus.DESTROYED,
            }
            return status_map.get(status, ContainerStatus.DESTROYED)

        except Exception:
            return None

    async def execute(
        self,
        container_id: str,
        prompt: str,
        session_id: str | None = None,
        timeout: int | None = None,
    ) -> ExecutionResult:
        """Execute a prompt in the agent container.

        Args:
            container_id: Container to execute in
            prompt: Prompt to send to Claude
            session_id: Optional session ID for conversation continuation
            timeout: Execution timeout in seconds

        Returns:
            ExecutionResult with output and new session ID
        """
        timeout = timeout or self.settings.default_timeout_sec

        # Build command for claude CLI
        # Using docker exec to run command in existing container
        cmd = [
            "docker",
            "exec",
            container_id,
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
        ]

        if session_id:
            cmd.extend(["--resume", session_id])

        logger.info(
            "agent_executing",
            container_id=container_id[:12],
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
            exit_code = proc.returncode or 0

            # Try to parse JSON output for session ID
            new_session_id = session_id
            result_text = output

            try:
                parsed = json.loads(output)
                new_session_id = parsed.get("session_id", session_id)
                result_text = parsed.get("result", output)
            except json.JSONDecodeError:
                pass  # Not JSON output, use raw

            success = exit_code == 0

            logger.info(
                "agent_execution_complete",
                container_id=container_id[:12],
                success=success,
                exit_code=exit_code,
                output_length=len(result_text),
            )

            return ExecutionResult(
                success=success,
                output=result_text,
                session_id=new_session_id,
                exit_code=exit_code,
                error=stderr.decode() if stderr and not success else None,
            )

        except Exception as e:
            logger.error(
                "agent_execution_error",
                container_id=container_id[:12],
                error=str(e),
                error_type=type(e).__name__,
            )
            return ExecutionResult(
                success=False,
                output="",
                error=str(e),
                exit_code=-1,
            )

    async def _docker_command(self, *args: str) -> None:
        """Execute a docker command."""
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode() if stderr else "Unknown error"
            raise RuntimeError(f"Docker command failed: {error_msg}")
