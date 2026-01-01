"""Container service - Docker management via subprocess."""

import asyncio
from datetime import UTC, datetime
import json
import uuid

import structlog

from workers_spawner.config import get_settings
from workers_spawner.config_parser import ConfigParser
from workers_spawner.models import AgentStatus, WorkerConfig

logger = structlog.get_logger()


class ExecutionResult:
    """Result of command execution in container."""

    def __init__(
        self,
        success: bool,
        output: str,
        exit_code: int = 0,
        error: str | None = None,
    ):
        self.success = success
        self.output = output
        self.exit_code = exit_code
        self.error = error


class ContainerService:
    """Manages Docker containers for CLI agents using subprocess."""

    def __init__(self):
        self.settings = get_settings()
        self._containers: dict[str, dict] = {}  # agent_id -> metadata

    async def create_container(
        self, config: WorkerConfig, context: dict[str, str] | None = None
    ) -> str:
        """Create a new agent container.

        Args:
            config: Worker configuration
            context: Additional context (user_id, project_id, etc.)

        Returns:
            agent_id: Unique identifier for the container
        """
        agent_id = f"agent-{uuid.uuid4().hex[:12]}"
        parser = ConfigParser(config)

        # Validate config
        errors = parser.validate()
        if errors:
            raise ValueError(f"Invalid config: {errors}")

        # Build docker run command
        cmd = [
            "docker",
            "run",
            "-d",  # Detached mode
            f"--name={agent_id}",
            f"--network={self.settings.container_network}",
        ]

        # Add environment variables
        env_vars = parser.get_env_vars()
        for key, value in env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add context as env vars
        if context:
            for key, value in context.items():
                cmd.extend(["-e", f"ORCHESTRATOR_{key.upper()}={value}"])

        # Add install commands as JSON env var
        install_commands = parser.get_install_commands()
        if install_commands:
            cmd.extend(["-e", f"INSTALL_COMMANDS={json.dumps(install_commands)}"])

        # Add agent command
        agent_command = parser.get_agent_command()
        cmd.extend(["-e", f"AGENT_COMMAND={agent_command}"])

        # Add image
        cmd.append(self.settings.worker_image)

        # Mount session volume if requested and configured
        if config.mount_session_volume:
            if self.settings.host_claude_dir:
                # Mount to /home/worker/.claude (non-root user home)
                cmd.extend(["-v", f"{self.settings.host_claude_dir}:/home/worker/.claude"])
                logger.info(
                    "mounting_session_volume",
                    agent_id=agent_id,
                    host_path=self.settings.host_claude_dir,
                )
            else:
                logger.warning(
                    "session_volume_mount_skipped",
                    agent_id=agent_id,
                    reason="HOST_CLAUDE_DIR not set",
                )

        logger.info(
            "creating_container",
            agent_id=agent_id,
            agent_type=config.agent.value,
            capabilities=[c.value for c in config.capabilities],
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error = stderr.decode() if stderr else "Unknown error"
                logger.error("container_create_failed", agent_id=agent_id, error=error)
                raise RuntimeError(f"Failed to create container: {error}")

            # Store metadata
            self._containers[agent_id] = {
                "config": config,
                "created_at": datetime.now(UTC).isoformat(),
                "state": "running",
                "last_activity": datetime.now(UTC).isoformat(),
                "ttl_hours": config.ttl_hours,
            }

            logger.info("container_created", agent_id=agent_id)
            return agent_id

        except Exception as e:
            logger.error(
                "container_create_error",
                agent_id=agent_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            raise

    async def send_command(
        self, agent_id: str, command: str, timeout: int | None = None
    ) -> ExecutionResult:
        """Execute a command in the container.

        Uses docker exec to run the command in the agent's shell.
        """
        timeout = timeout or self.settings.default_timeout_sec

        cmd = [
            "docker",
            "exec",
            agent_id,
            "/bin/bash",
            "-c",
            command,
        ]

        logger.info(
            "executing_command",
            agent_id=agent_id,
            command_length=len(command),
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
                    exit_code=-1,
                    error=f"Command timed out after {timeout}s",
                )

            output = stdout.decode() if stdout else ""
            stderr_text = stderr.decode() if stderr else ""
            exit_code = proc.returncode or 0

            # Update last activity
            if agent_id in self._containers:
                self._containers[agent_id]["last_activity"] = datetime.now(UTC).isoformat()

            success = exit_code == 0

            logger.info(
                "command_complete",
                agent_id=agent_id,
                success=success,
                exit_code=exit_code,
                output_length=len(output),
            )

            return ExecutionResult(
                success=success,
                output=output,
                exit_code=exit_code,
                error=stderr_text if not success else None,
            )

        except Exception as e:
            logger.error(
                "command_execution_error",
                agent_id=agent_id,
                error=str(e),
            )
            return ExecutionResult(
                success=False,
                output="",
                exit_code=-1,
                error=str(e),
            )

    async def send_file(self, agent_id: str, path: str, content: str) -> bool:
        """Write a file to the container.

        Uses docker exec with echo/cat to write content.
        """
        # Escape content for shell
        escaped_content = content.replace("'", "'\\''")

        cmd = [
            "docker",
            "exec",
            agent_id,
            "/bin/bash",
            "-c",
            f"mkdir -p $(dirname '{path}') && echo '{escaped_content}' > '{path}'",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                error = stderr.decode() if stderr else "Unknown error"
                logger.error("send_file_failed", agent_id=agent_id, path=path, error=error)
                return False

            logger.info("file_sent", agent_id=agent_id, path=path)
            return True

        except Exception as e:
            logger.error("send_file_error", agent_id=agent_id, path=path, error=str(e))
            return False

    async def get_status(self, agent_id: str) -> AgentStatus | None:
        """Get container status via docker inspect."""
        cmd = ["docker", "inspect", "--format", "{{.State.Status}}", agent_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode != 0:
                return None

            docker_state = stdout.decode().strip()

            # Map docker state to our state
            state_map = {
                "running": "running",
                "paused": "paused",
                "exited": "stopped",
                "dead": "error",
            }
            state = state_map.get(docker_state, "unknown")

            metadata = self._containers.get(agent_id, {})

            return AgentStatus(
                agent_id=agent_id,
                state=state,
                created_at=metadata.get("created_at", "unknown"),
                last_activity=metadata.get("last_activity"),
                ttl_remaining_sec=None,  # TODO: calculate from TTL
            )

        except Exception as e:
            logger.error("get_status_error", agent_id=agent_id, error=str(e))
            return None

    async def get_logs(self, agent_id: str, tail: int = 100) -> str:
        """Get container logs."""
        cmd = ["docker", "logs", "--tail", str(tail), agent_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            # Docker logs outputs to both stdout and stderr
            output = stdout.decode() if stdout else ""
            stderr_output = stderr.decode() if stderr else ""

            return output + stderr_output

        except Exception as e:
            logger.error("get_logs_error", agent_id=agent_id, error=str(e))
            return ""

    async def delete(self, agent_id: str) -> bool:
        """Stop and remove container."""
        cmd = ["docker", "rm", "-f", agent_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                error = stderr.decode() if stderr else "Unknown error"
                logger.error("delete_failed", agent_id=agent_id, error=error)
                return False

            # Remove from metadata
            self._containers.pop(agent_id, None)

            logger.info("container_deleted", agent_id=agent_id)
            return True

        except Exception as e:
            logger.error("delete_error", agent_id=agent_id, error=str(e))
            return False

    async def pause(self, agent_id: str) -> bool:
        """Pause a running container."""
        cmd = ["docker", "pause", agent_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            if proc.returncode == 0:
                if agent_id in self._containers:
                    self._containers[agent_id]["state"] = "paused"
                logger.info("container_paused", agent_id=agent_id)
                return True

            return False

        except Exception as e:
            logger.error("pause_error", agent_id=agent_id, error=str(e))
            return False

    async def unpause(self, agent_id: str) -> bool:
        """Unpause a paused container."""
        cmd = ["docker", "unpause", agent_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            if proc.returncode == 0:
                if agent_id in self._containers:
                    self._containers[agent_id]["state"] = "running"
                logger.info("container_unpaused", agent_id=agent_id)
                return True

            return False

        except Exception as e:
            logger.error("unpause_error", agent_id=agent_id, error=str(e))
            return False
