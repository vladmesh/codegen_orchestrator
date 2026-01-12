import asyncio
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

    async def execute_agent(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the actual agent logic.
        In real usage, this might spawn a subprocess or call an LLM.
        For verification/integration, this method can be mocked.
        """
        # Placeholder implementation
        logger.info("executing_agent_placeholder", data=data)
        await asyncio.sleep(0.1)  # Simulate work
        return {"result": f"Processed {data.get('content', 'unknown')}"}

    async def publish_lifecycle(
        self, status: str, ref_msg_id: str, result: dict = None, error: str = None
    ):
        """Publish lifecycle event."""
        # Use shared contract
        from shared.contracts.queues.worker_lifecycle import WorkerLifecycleEvent

        # event literal: "started", "completed", "failed", "stopped"
        # status map might be needed if strings differ

        event = WorkerLifecycleEvent(
            worker_id=self.config.consumer_name,  # using consumer name as worker_id
            event=status,
            result=result,
            error=error,
        )

        await self.redis.publish_message("worker:lifecycle", event)
