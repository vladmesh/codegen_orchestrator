"""Container service - Docker management via subprocess."""

import asyncio
from datetime import UTC, datetime
import json
import os
import uuid

import structlog

from workers_spawner.config import get_settings
from workers_spawner.config_parser import ConfigParser
from workers_spawner.models import AgentStatus, WorkerConfig

logger = structlog.get_logger()

# Command preview length for logging
COMMAND_PREVIEW_LENGTH = 200


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

        # Initialize session manager
        import redis.asyncio as redis

        from workers_spawner.session_manager import AgentSessionManager

        self.redis = redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_manager = AgentSessionManager(self.redis)

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
        parser = ConfigParser(config, self)

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

        # Add Sysbox runtime if Docker capability is enabled (Docker-in-Docker)
        from workers_spawner.models import CapabilityType

        if CapabilityType.DOCKER in config.capabilities:
            cmd.append("--runtime=sysbox-runc")
            logger.info(
                "enabling_docker_in_docker",
                agent_id=agent_id,
                runtime="sysbox-runc",
            )

        # Add environment variables
        env_vars = parser.get_env_vars()

        # Auto-inject required env vars from spawner's environment if not provided
        # This allows secrets like ANTHROPIC_API_KEY to be set in spawner's .env
        # without clients needing to know about them
        #
        # EXCEPTION: Don't inject ANTHROPIC_API_KEY when using session volume mount,
        # as Claude Code will use the OAuth session from ~/.claude/.credentials.json
        required_vars = parser.get_required_env_vars()
        missing_vars = []
        for required_var in required_vars:
            if required_var not in env_vars:
                # Skip ANTHROPIC_API_KEY if mounting session (OAuth takes precedence)
                if required_var == "ANTHROPIC_API_KEY" and config.mount_session_volume:
                    logger.info(
                        "skipping_api_key_for_session",
                        agent_id=agent_id,
                        reason="Using OAuth session from mounted volume",
                    )
                    continue

                value_from_env = os.environ.get(required_var)
                if value_from_env:
                    env_vars[required_var] = value_from_env
                    logger.debug(
                        "auto_injected_env_var",
                        agent_id=agent_id,
                        var_name=required_var,
                    )
                else:
                    missing_vars.append(required_var)

        # Validate all required vars are present
        if missing_vars:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing_vars)}. "
                "Set them in spawner's .env or pass in config.env_vars"
            )

        for key, value in env_vars.items():
            cmd.extend(["-e", f"{key}={value}"])

        # Add context as env vars
        if context:
            for key, value in context.items():
                cmd.extend(["-e", f"ORCHESTRATOR_{key.upper()}={value}"])

        # Add orchestrator env vars for CLI communication
        # These are required for `orchestrator respond` CLI to work inside containers
        cmd.extend(["-e", f"ORCHESTRATOR_AGENT_ID={agent_id}"])
        cmd.extend(["-e", f"ORCHESTRATOR_REDIS_URL={self.settings.redis_url}"])
        cmd.extend(["-e", f"ORCHESTRATOR_API_URL={self.settings.api_url}"])

        # Add install commands as JSON env var
        install_commands = parser.get_install_commands()
        if install_commands:
            cmd.extend(["-e", f"INSTALL_COMMANDS={json.dumps(install_commands)}"])

        # Add agent command
        agent_command = parser.get_agent_command()
        cmd.extend(["-e", f"AGENT_COMMAND={agent_command}"])

        # Mount session volume if requested and configured
        # IMPORTANT: Must be before image name in docker run command
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

        # Add image (must be last before entrypoint args)
        cmd.append(self.settings.worker_image)

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

            # Wait for container to be ready (entrypoint to complete)
            await self._wait_for_container_ready(agent_id)

            # Create setup files (CLAUDE.md, AGENTS.md, etc.)
            setup_files = parser.get_setup_files()
            for file_path, content in setup_files.items():
                success = await self.send_file(agent_id, file_path, content)
                if not success:
                    logger.warning(
                        "setup_file_failed",
                        agent_id=agent_id,
                        file_path=file_path,
                    )

            # Run capability-specific setup commands
            await self._run_capability_setup(agent_id, config, env_vars)

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
        Uses -i flag to inherit ENV vars from container.
        """
        timeout = timeout or self.settings.default_timeout_sec

        cmd = [
            "docker",
            "exec",
            "-i",  # Interactive mode inherits ENV vars
            agent_id,
            "/bin/bash",
            "-l",  # Login shell to load .profile/.bashrc (PATH with npm global)
            "-c",
            command,
        ]

        logger.info(
            "executing_command",
            agent_id=agent_id,
            command_length=len(command),
            command_preview=command[:COMMAND_PREVIEW_LENGTH]
            if len(command) > COMMAND_PREVIEW_LENGTH
            else command,
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
        # Clean up session context
        await self.session_manager.delete_session_context(agent_id)

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

    async def _wait_for_container_ready(
        self, agent_id: str, max_attempts: int = 30, retry_delay: float = 0.5
    ) -> None:
        """Wait for container to be ready (entrypoint to complete).

        Tests readiness by executing a simple command.
        Retries until success or max attempts reached.

        Args:
            agent_id: Container ID
            max_attempts: Maximum number of retry attempts (default: 30)
            retry_delay: Delay between retries in seconds (default: 0.5s)

        Raises:
            RuntimeError: If container not ready after max attempts
        """
        logger.info("waiting_for_container_ready", agent_id=agent_id)

        for attempt in range(1, max_attempts + 1):
            # Readiness check: verify agent command is available
            # This ensures npm install has completed
            cmd = ["docker", "exec", agent_id, "/bin/bash", "-c", "which claude || echo waiting"]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()

                if proc.returncode == 0:
                    output = stdout.decode().strip()
                    # Check if we got actual claude path, not just "waiting"
                    if output and "claude" in output and output != "waiting":
                        logger.info(
                            "container_ready",
                            agent_id=agent_id,
                            attempts=attempt,
                            elapsed_sec=attempt * retry_delay,
                        )
                        return

                # Non-zero exit code, container not ready yet
                logger.debug(
                    "container_not_ready_yet",
                    agent_id=agent_id,
                    attempt=attempt,
                    exit_code=proc.returncode,
                )

            except Exception as e:
                logger.debug(
                    "readiness_check_failed",
                    agent_id=agent_id,
                    attempt=attempt,
                    error=str(e),
                )

            # Wait before next attempt
            if attempt < max_attempts:
                await asyncio.sleep(retry_delay)

        # Max attempts reached
        raise RuntimeError(
            f"Container {agent_id} not ready after {max_attempts} attempts "
            f"({max_attempts * retry_delay}s)"
        )

    async def _run_capability_setup(
        self, agent_id: str, config: WorkerConfig, env_vars: dict[str, str]
    ) -> None:
        """Run post-creation setup for capabilities.

        Some capabilities need to run commands after container creation
        (e.g., GitHub needs to setup git credentials).

        Args:
            agent_id: Container ID
            config: Worker configuration
            env_vars: Environment variables dict
        """
        from workers_spawner.models import CapabilityType

        # GitHub capability: setup git credentials
        if CapabilityType.GITHUB in config.capabilities:
            from workers_spawner.factories.capabilities.github import get_github_setup_commands

            commands = get_github_setup_commands(env_vars)
            if commands:
                logger.info(
                    "running_github_setup",
                    agent_id=agent_id,
                    num_commands=len(commands),
                )
                for cmd in commands:
                    try:
                        result = await self.send_command(agent_id, cmd, timeout=10)
                        if not result.success:
                            logger.warning(
                                "github_setup_command_failed",
                                agent_id=agent_id,
                                command=cmd[:50],
                                error=result.error,
                            )
                    except Exception as e:
                        logger.warning(
                            "github_setup_exception",
                            agent_id=agent_id,
                            command=cmd[:50],
                            error=str(e),
                        )
