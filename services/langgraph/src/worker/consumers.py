"""Redis Stream consumers for LangGraph orchestrator.

Consumes messages from:
- engineering:queue -> Triggers Engineering Flow
- deploy:queue -> Triggers Deploy Flow
- scaffolder:results -> Resumes graph after scaffolding
- worker:responses:developer -> Resumes graph after worker creation
- worker:developer:output -> Resumes graph after worker task completion
"""

import asyncio
from collections.abc import Awaitable, Callable

import redis.asyncio as redis
import structlog

from shared.contracts.queues.deploy import DeployMessage
from shared.contracts.queues.developer_worker import DeveloperWorkerOutput
from shared.contracts.queues.engineering import EngineeringMessage
from shared.contracts.queues.scaffolder import ScaffolderResult
from shared.contracts.queues.worker import CreateWorkerResponse

from ..config.settings import get_settings

logger = structlog.get_logger()

# Consumer group name for LangGraph service
CONSUMER_GROUP = "langgraph-service"


class StreamConsumer:
    """Generic Redis Stream consumer with consumer group support."""

    def __init__(
        self,
        redis_client: redis.Redis,
        stream_name: str,
        handler: Callable[[dict], Awaitable[None]],
        consumer_name: str = "consumer-1",
    ):
        self.redis = redis_client
        self.stream = stream_name
        self.handler = handler
        self.consumer_name = consumer_name
        self.running = False

    async def ensure_group(self) -> None:
        """Ensure consumer group exists."""
        try:
            await self.redis.xgroup_create(self.stream, CONSUMER_GROUP, id="0", mkstream=True)
            logger.info(
                "consumer_group_created",
                stream=self.stream,
                group=CONSUMER_GROUP,
            )
        except redis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                # Group already exists
                pass
            else:
                raise

    async def run(self) -> None:
        """Run consumer loop."""
        await self.ensure_group()
        self.running = True

        logger.info(
            "stream_consumer_started",
            stream=self.stream,
            group=CONSUMER_GROUP,
            consumer=self.consumer_name,
        )

        while self.running:
            try:
                # Read new messages (block for 5 seconds)
                messages = await self.redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=self.consumer_name,
                    streams={self.stream: ">"},
                    count=1,
                    block=5000,
                )

                if not messages:
                    continue

                for _stream_name, stream_messages in messages:
                    for msg_id, fields in stream_messages:
                        try:
                            # Decode bytes if needed
                            decoded = {
                                (k.decode() if isinstance(k, bytes) else k): (
                                    v.decode() if isinstance(v, bytes) else v
                                )
                                for k, v in fields.items()
                            }

                            await self.handler(decoded)

                            # Acknowledge message
                            await self.redis.xack(self.stream, CONSUMER_GROUP, msg_id)
                            logger.debug(
                                "message_processed",
                                stream=self.stream,
                                msg_id=msg_id,
                            )
                        except Exception as e:
                            logger.error(
                                "message_processing_failed",
                                stream=self.stream,
                                msg_id=msg_id,
                                error=str(e),
                                error_type=type(e).__name__,
                                exc_info=True,
                            )
                            # Don't ACK - message will be retried

            except asyncio.CancelledError:
                logger.info("stream_consumer_cancelled", stream=self.stream)
                break
            except redis.ResponseError as e:
                if "NOGROUP" in str(e):
                    # Stream was reset (e.g., by flushdb) - recreate group
                    logger.warning(
                        "consumer_group_lost_recreating",
                        stream=self.stream,
                        group=CONSUMER_GROUP,
                    )
                    await self.ensure_group()
                else:
                    logger.error(
                        "stream_consumer_redis_error",
                        stream=self.stream,
                        error=str(e),
                        error_type=type(e).__name__,
                    )
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(
                    "stream_consumer_error",
                    stream=self.stream,
                    error=str(e),
                    error_type=type(e).__name__,
                    exc_info=True,
                )
                await asyncio.sleep(1)  # Backoff on error

    def stop(self) -> None:
        """Stop the consumer loop."""
        self.running = False


class LangGraphConsumers:
    """Manages all Redis Stream consumers for LangGraph service."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis: redis.Redis | None = None
        self.consumers: list[StreamConsumer] = []

        # Callbacks for graph resumption (to be set by graph runner)
        self.on_engineering_message: Callable[[EngineeringMessage], Awaitable[None]] | None = None
        self.on_deploy_message: Callable[[DeployMessage], Awaitable[None]] | None = None
        self.on_scaffolder_result: Callable[[ScaffolderResult], Awaitable[None]] | None = None
        self.on_worker_created: Callable[[CreateWorkerResponse], Awaitable[None]] | None = None
        self.on_worker_output: Callable[[DeveloperWorkerOutput], Awaitable[None]] | None = None

    async def start(self) -> None:
        """Initialize Redis connection and start all consumers."""
        self.redis = redis.from_url(self.redis_url, decode_responses=False)

        # Engineering queue consumer
        self.consumers.append(
            StreamConsumer(
                self.redis,
                "engineering:queue",
                self._handle_engineering_message,
            )
        )

        # Deploy queue consumer
        self.consumers.append(
            StreamConsumer(
                self.redis,
                "deploy:queue",
                self._handle_deploy_message,
            )
        )

        # Scaffolder results consumer
        self.consumers.append(
            StreamConsumer(
                self.redis,
                "scaffolder:results",
                self._handle_scaffolder_result,
            )
        )

        # Worker responses consumer (for worker creation responses)
        self.consumers.append(
            StreamConsumer(
                self.redis,
                "worker:responses:developer",
                self._handle_worker_created,
            )
        )

        # Worker output consumer (for task results)
        self.consumers.append(
            StreamConsumer(
                self.redis,
                "worker:developer:output",
                self._handle_worker_output,
            )
        )

        # Start all consumers concurrently
        await asyncio.gather(*[c.run() for c in self.consumers])

    async def stop(self) -> None:
        """Stop all consumers and close Redis connection."""
        for consumer in self.consumers:
            consumer.stop()

        if self.redis:
            await self.redis.close()

    # Handler implementations
    async def _handle_engineering_message(self, fields: dict) -> None:
        """Handle incoming engineering request."""
        data = fields.get("data", "{}")
        message = EngineeringMessage.model_validate_json(data)

        logger.info(
            "engineering_message_received",
            task_id=message.task_id,
            project_id=message.project_id,
        )

        if self.on_engineering_message:
            await self.on_engineering_message(message)

    async def _handle_deploy_message(self, fields: dict) -> None:
        """Handle incoming deploy request."""
        data = fields.get("data", "{}")
        message = DeployMessage.model_validate_json(data)

        logger.info(
            "deploy_message_received",
            task_id=message.task_id,
            project_id=message.project_id,
        )

        if self.on_deploy_message:
            await self.on_deploy_message(message)

    async def _handle_scaffolder_result(self, fields: dict) -> None:
        """Handle scaffolder completion result."""
        data = fields.get("data", "{}")
        result = ScaffolderResult.model_validate_json(data)

        logger.info(
            "scaffolder_result_received",
            project_id=result.project_id,
            status=result.status,
        )

        if self.on_scaffolder_result:
            await self.on_scaffolder_result(result)

    async def _handle_worker_created(self, fields: dict) -> None:
        """Handle worker creation response."""
        data = fields.get("data", "{}")
        response = CreateWorkerResponse.model_validate_json(data)

        logger.info(
            "worker_created_response_received",
            request_id=response.request_id,
            success=response.success,
            worker_id=response.worker_id,
        )

        if self.on_worker_created:
            await self.on_worker_created(response)

    async def _handle_worker_output(self, fields: dict) -> None:
        """Handle worker task output."""
        data = fields.get("data", "{}")
        output = DeveloperWorkerOutput.model_validate_json(data)

        logger.info(
            "worker_output_received",
            request_id=output.request_id,
            status=output.status,
            task_id=output.task_id,
        )

        if self.on_worker_output:
            await self.on_worker_output(output)


async def run_consumers() -> None:
    """Run all LangGraph consumers."""
    settings = get_settings()
    consumers = LangGraphConsumers(settings.redis_url)

    # Initialize graph runner with publisher
    from .graph_runner import GraphRunner
    from .redis_publisher import RedisPublisher

    publisher = RedisPublisher(settings.redis_url)
    runner = GraphRunner(publisher)

    # Wire up callbacks
    consumers.on_engineering_message = runner.start_engineering_flow
    consumers.on_deploy_message = runner.start_deploy_flow
    consumers.on_scaffolder_result = runner.resume_after_scaffolding
    consumers.on_worker_created = runner.resume_after_worker_created
    consumers.on_worker_output = runner.resume_after_worker_output

    try:
        await consumers.start()
    finally:
        await publisher.close()
        await consumers.stop()
