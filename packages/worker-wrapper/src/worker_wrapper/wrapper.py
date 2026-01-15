import asyncio
import json
from typing import Any

import structlog

from shared.redis.client import RedisStreamClient

from .config import WorkerWrapperConfig

logger = structlog.get_logger(__name__)


class WorkerWrapper:
    """
    Wraps a worker agent process, handling Redis Stream communication
    and lifecycle management.
    """

    def __init__(self, config: WorkerWrapperConfig, redis_client: RedisStreamClient | None = None):
        self.config = config
        if redis_client:
            self.redis = redis_client
            self._owns_redis = False
        else:
            self.redis = RedisStreamClient(redis_url=config.redis_url)
            self._owns_redis = True
        self._running = False
        self._task: asyncio.Task | None = None

    async def run(self):
        """Main loop: connect, consume, execute, publish."""
        self._running = True
        logger.info("worker_wrapper_starting", config=self.config.model_dump())

        await self.redis.connect()
        # Ensure group exists
        await self.redis.ensure_consumer_group(self.config.input_stream, self.config.consumer_group)

        try:
            async for message in self.redis.consume(
                stream=self.config.input_stream,
                group=self.config.consumer_group,
                consumer=self.config.consumer_name,
                block_ms=self.config.poll_interval_ms,
            ):
                if not self._running:
                    break

                if message is None:
                    # Timeout/No message, continue loop
                    continue

                await self.process_message(message)

        except asyncio.CancelledError:
            logger.info("worker_wrapper_cancelled")
        except Exception as e:
            logger.exception("worker_wrapper_crashed", error=str(e))
            raise
        finally:
            if self._owns_redis:
                await self.redis.close()
                logger.info("worker_wrapper_stopped")

    async def process_message(self, message):
        """Process a single task message."""
        msg_id = message.message_id
        data = message.data

        logger.info("processing_task", msg_id=msg_id)

        # Persist task context for crash recovery (Gap B)
        # We save task_id/request_id so DockerEventsListener can read them if container dies
        context_update = {}
        if "task_id" in data:
            context_update["task_id"] = data["task_id"]
        if "request_id" in data:
            context_update["request_id"] = data["request_id"]

        if context_update:
            # Access raw redis client to use hset
            await self.redis.redis.hset(
                f"worker:status:{self.config.consumer_name}", mapping=context_update
            )

        # 1. Lifecycle: Started
        await self.publish_lifecycle("started", msg_id)

        # 2. Execute
        try:
            result = await self.execute_agent(data)
            status = "completed"
            error = None
        except Exception as e:
            logger.error("execution_failed", error=str(e))
            result = None
            error = str(e)
            status = "failed"

        # 3. Publish Result (if successful, or error result?)
        # Convention: Output stream gets result or error wrapped?
        # Usually output stream is for next step in graph.
        if result:
            await self.redis.publish(self.config.output_stream, result)

        # 4. Lifecycle: Completed/Failed
        await self.publish_lifecycle(status, msg_id, result=result, error=error)

    async def execute_agent(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """
        Execute the agent using the configured runner and parsing logic.
        """
        # 1. Get Session
        # We need raw redis client for session manager
        # RedisStreamClient exposes .redis property which is redis.Redis
        from .session import SessionManager

        session_manager = SessionManager(
            redis=self.redis.redis, worker_id=self.config.consumer_name
        )

        # Gap solution: Claude CLI manages its own session IDs and doesn't accept random ones.
        # So for Claude, we don't create a new random ID.
        create_new_session = self.config.agent_type != "claude"
        session_id = await session_manager.get_or_create_session(create_new=create_new_session)

        # 2. Select Runner
        from .runners.claude import ClaudeRunner
        from .runners.factory import FactoryRunner

        if self.config.agent_type == "claude":
            runner = ClaudeRunner(session_id=session_id)
        elif self.config.agent_type == "factory":
            runner = FactoryRunner()  # Factory runner might not need session or handled differently
        else:
            raise ValueError(f"Unknown agent type: {self.config.agent_type}")

        # 3. Build Command
        prompt = data.get("content", "")
        if not prompt:
            # If no content, maybe just keep alive or error?
            # For now assume content is required
            raise ValueError("Task data missing 'content'")

        cmd = runner.build_command(prompt=prompt)
        logger.info("executing_agent_command", cmd=cmd)

        # 4. Execute Subprocess
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode().strip()
        stderr = stderr_bytes.decode().strip()

        if proc.returncode != 0:
            logger.error(
                "agent_process_failed", stderr=stderr, stdout=stdout, exit_code=proc.returncode
            )
            raise RuntimeError(f"Agent process failed with code {proc.returncode}: {stderr}")

        # 5. Capture session_id from Claude CLI JSON output
        # Claude CLI with --output-format json returns session_id in the response
        if self.config.agent_type == "claude" and not session_id:
            captured_session_id = self._extract_session_id_from_output(stdout)
            if captured_session_id:
                logger.info("captured_claude_session_from_output", session_id=captured_session_id)
                await session_manager.update_session(captured_session_id)

        # 6. Parse Result
        from .result_parser import ResultParseError, ResultParser

        try:
            result = ResultParser.parse(stdout)
            if result is None:
                logger.warning("no_result_tags_found", stdout=stdout)
                # Maybe return raw stdout as fallback?
                # Protocol says we return dict.
                return {"raw_output": stdout, "status": "no_structured_result"}
            return result
        except ResultParseError as e:
            logger.error("result_parsing_failed", error=str(e), stdout=stdout)
            raise

    def _extract_session_id_from_output(self, stdout: str) -> str | None:
        """
        Extract session_id from Claude CLI JSON output.

        Claude CLI with --output-format json returns:
        {
            "type": "result",
            "session_id": "uuid-here",
            ...
        }
        """
        try:
            # stdout may contain multiple JSON objects (streaming), find the result one
            # Try parsing the whole output first
            data = json.loads(stdout)
            if isinstance(data, dict) and "session_id" in data:
                return data["session_id"]
        except json.JSONDecodeError:
            # Try to find JSON objects line by line
            for line in stdout.split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and "session_id" in data:
                        return data["session_id"]
                except json.JSONDecodeError:
                    continue
        except Exception as e:
            logger.warning("failed_to_extract_session_id", error=str(e))

        return None

    async def publish_lifecycle(
        self, status: str, ref_msg_id: str, result: dict = None, error: str = None
    ):
        """Publish lifecycle event."""
        # Use shared contract
        from shared.contracts.queues.worker_lifecycle import WorkerLifecycleEvent

        event = WorkerLifecycleEvent(
            worker_id=self.config.consumer_name,  # using consumer name as worker_id
            event=status,
            result=result,
            error=error,
        )

        await self.redis.publish_message("worker:lifecycle", event)
