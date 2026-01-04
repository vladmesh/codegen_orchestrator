"""Process manager for persistent CLI agent processes.

Manages long-running agent processes with stdin/stdout communication
instead of ephemeral docker exec per message.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from workers_spawner.factories.base import AgentFactory

logger = structlog.get_logger()


@dataclass
class ProcessHandle:
    """Handle to a running agent process."""

    process: asyncio.subprocess.Process
    factory: "AgentFactory"
    agent_id: str
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_alive(self) -> bool:
        """Check if process is still running."""
        return self.process.returncode is None


class ProcessManager:
    """Manages persistent CLI agent processes.

    Instead of spawning a new process for each message (ephemeral),
    this manager maintains long-running processes that communicate
    via stdin/stdout.
    """

    def __init__(self) -> None:
        self._processes: dict[str, ProcessHandle] = {}
        self._read_locks: dict[str, asyncio.Lock] = {}
        self._write_locks: dict[str, asyncio.Lock] = {}

    async def start_process(
        self,
        agent_id: str,
        factory: "AgentFactory",
    ) -> None:
        """Start a persistent agent process via docker exec -i.

        Args:
            agent_id: Container/agent ID
            factory: AgentFactory to get command from
        """
        if agent_id in self._processes:
            logger.warning("process_already_running", agent_id=agent_id)
            return

        # Get persistent command from factory
        command = factory.get_persistent_command()

        # Build docker exec command for interactive mode
        cmd = [
            "docker",
            "exec",
            "-i",  # Interactive (keep stdin open)
            agent_id,
            "/bin/bash",
            "-l",  # Login shell (load PATH)
            "-c",
            command,
        ]

        logger.info(
            "starting_persistent_process",
            agent_id=agent_id,
            command=command,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            self._processes[agent_id] = ProcessHandle(
                process=process,
                factory=factory,
                agent_id=agent_id,
            )
            self._read_locks[agent_id] = asyncio.Lock()
            self._write_locks[agent_id] = asyncio.Lock()

            logger.info(
                "persistent_process_started",
                agent_id=agent_id,
                pid=process.pid,
            )

        except Exception as e:
            logger.error(
                "failed_to_start_process",
                agent_id=agent_id,
                error=str(e),
            )
            raise RuntimeError(f"Failed to start process for {agent_id}: {e}") from e

    async def write_to_stdin(self, agent_id: str, message: str) -> None:
        """Write formatted message to agent's stdin.

        Args:
            agent_id: Container/agent ID
            message: Raw message text (will be formatted by factory)

        Raises:
            RuntimeError: If agent not found or process dead
        """
        handle = self._processes.get(agent_id)
        if not handle:
            raise RuntimeError(f"No process found for agent {agent_id}")

        if not handle.is_alive:
            raise RuntimeError(f"Process for agent {agent_id} is dead")

        if handle.process.stdin is None:
            raise RuntimeError(f"No stdin available for agent {agent_id}")

        # Format message using factory
        formatted = handle.factory.format_message_for_stdin(message)

        async with self._write_locks[agent_id]:
            handle.process.stdin.write(formatted.encode())
            await handle.process.stdin.drain()

            logger.debug(
                "wrote_to_stdin",
                agent_id=agent_id,
                message_length=len(message),
            )

    async def read_stdout_line(self, agent_id: str, timeout: float = 0.1) -> str | None:
        """Read one line from stdout (non-blocking with timeout).

        Args:
            agent_id: Container/agent ID
            timeout: Seconds to wait for data (default: 0.1s)

        Returns:
            Line of text (without newline), or None if no data available
        """
        handle = self._processes.get(agent_id)
        if not handle or handle.process.stdout is None:
            return None

        async with self._read_locks[agent_id]:
            try:
                line_bytes = await asyncio.wait_for(
                    handle.process.stdout.readline(),
                    timeout=timeout,
                )
                if line_bytes:
                    return line_bytes.decode().rstrip("\n")
                return None
            except TimeoutError:
                return None

    async def read_stderr_line(self, agent_id: str, timeout: float = 0.1) -> str | None:
        """Read one line from stderr (non-blocking with timeout).

        Args:
            agent_id: Container/agent ID
            timeout: Seconds to wait for data (default: 0.1s)

        Returns:
            Line of text (without newline), or None if no data available
        """
        handle = self._processes.get(agent_id)
        if not handle or handle.process.stderr is None:
            return None

        try:
            line_bytes = await asyncio.wait_for(
                handle.process.stderr.readline(),
                timeout=timeout,
            )
            if line_bytes:
                return line_bytes.decode().rstrip("\n")
            return None
        except TimeoutError:
            return None

    async def stop_process(self, agent_id: str, timeout: float = 5.0) -> bool:
        """Gracefully stop agent process.

        Sends SIGTERM, waits for graceful shutdown, then SIGKILL if needed.

        Args:
            agent_id: Container/agent ID
            timeout: Seconds to wait for graceful shutdown

        Returns:
            True if stopped successfully
        """
        handle = self._processes.get(agent_id)
        if not handle:
            logger.debug("no_process_to_stop", agent_id=agent_id)
            return True

        process = handle.process

        if not handle.is_alive:
            logger.debug("process_already_dead", agent_id=agent_id)
            self._cleanup(agent_id)
            return True

        logger.info("stopping_process", agent_id=agent_id, pid=process.pid)

        # Close stdin first to signal EOF
        if process.stdin:
            process.stdin.close()
            await process.stdin.wait_closed()

        # Send SIGTERM
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=timeout)
            logger.info("process_terminated_gracefully", agent_id=agent_id)
        except TimeoutError:
            # Force kill
            logger.warning("process_force_kill", agent_id=agent_id)
            process.kill()
            await process.wait()

        self._cleanup(agent_id)
        return True

    def _cleanup(self, agent_id: str) -> None:
        """Remove agent from internal state."""
        self._processes.pop(agent_id, None)
        self._read_locks.pop(agent_id, None)
        self._write_locks.pop(agent_id, None)

    def get_handle(self, agent_id: str) -> ProcessHandle | None:
        """Get process handle for an agent."""
        return self._processes.get(agent_id)

    def list_agents(self) -> list[str]:
        """List all agents with active processes."""
        return list(self._processes.keys())

    def is_running(self, agent_id: str) -> bool:
        """Check if agent has a running process."""
        handle = self._processes.get(agent_id)
        return handle is not None and handle.is_alive
