"""Log collector for agent process stdout/stderr.

Simplified collector that stores logs without parsing tool calls.
Tool calls are handled by the agent via `orchestrator respond` CLI.
"""

import asyncio
from datetime import UTC, datetime

import redis.asyncio as redis
import structlog

from workers_spawner.process_manager import ProcessManager

logger = structlog.get_logger()


class LogCollector:
    """Collects stdout/stderr from agent processes and stores in Redis.

    This is a simplified collector that only stores logs.
    No parsing of tool calls - agents use `orchestrator respond` CLI
    to communicate, which writes directly to Redis.
    """

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client
        self._tasks: dict[str, asyncio.Task] = {}
        self._stop_events: dict[str, asyncio.Event] = {}

    async def start_collecting(
        self,
        agent_id: str,
        process_manager: ProcessManager,
        max_lines: int = 1000,
    ) -> None:
        """Start collecting logs for an agent in background.

        Args:
            agent_id: Agent ID to collect logs for
            process_manager: ProcessManager instance to read from
            max_lines: Maximum lines to keep in Redis stream
        """
        if agent_id in self._tasks:
            logger.warning("log_collector_already_running", agent_id=agent_id)
            return

        self._stop_events[agent_id] = asyncio.Event()

        task = asyncio.create_task(
            self._collect_loop(agent_id, process_manager, max_lines),
            name=f"log_collector_{agent_id}",
        )
        self._tasks[agent_id] = task

        logger.info("log_collector_started", agent_id=agent_id)

    async def stop_collecting(self, agent_id: str) -> None:
        """Stop collecting logs for an agent.

        Args:
            agent_id: Agent ID to stop collecting for
        """
        stop_event = self._stop_events.get(agent_id)
        if stop_event:
            stop_event.set()

        task = self._tasks.pop(agent_id, None)
        if task:
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._stop_events.pop(agent_id, None)
        logger.info("log_collector_stopped", agent_id=agent_id)

    async def _collect_loop(
        self,
        agent_id: str,
        process_manager: ProcessManager,
        max_lines: int,
    ) -> None:
        """Main collection loop for an agent."""
        stop_event = self._stop_events[agent_id]
        stream_key = f"agent:logs:{agent_id}"

        while not stop_event.is_set():
            # Read stdout
            stdout_line = await process_manager.read_stdout_line(agent_id, timeout=0.1)
            if stdout_line:
                await self._store_log(stream_key, "stdout", stdout_line, max_lines)

            # Read stderr
            stderr_line = await process_manager.read_stderr_line(agent_id, timeout=0.1)
            if stderr_line:
                await self._store_log(stream_key, "stderr", stderr_line, max_lines)

            # Check if process is still running
            if not process_manager.is_running(agent_id):
                logger.info("process_ended_stopping_collector", agent_id=agent_id)
                break

            # Small sleep if no data read
            if not stdout_line and not stderr_line:
                await asyncio.sleep(0.05)

    async def _store_log(
        self,
        stream_key: str,
        stream: str,
        line: str,
        max_lines: int,
    ) -> None:
        """Store a log line in Redis stream.

        Args:
            stream_key: Redis stream key
            stream: Stream type (stdout/stderr)
            line: Log line content
            max_lines: Maximum lines to keep
        """
        try:
            await self.redis.xadd(
                stream_key,
                {
                    "stream": stream,
                    "line": line,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                maxlen=max_lines,
            )
        except Exception as e:
            logger.error(
                "failed_to_store_log",
                stream_key=stream_key,
                error=str(e),
            )
