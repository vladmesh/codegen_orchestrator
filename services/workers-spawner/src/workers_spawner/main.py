"""Workers-Spawner service main entrypoint."""

import asyncio
import json

import redis.asyncio as redis
import structlog

from shared.logging_config import setup_logging
from workers_spawner.config import get_settings
from workers_spawner.container_service import ContainerService
from workers_spawner.events import EventPublisher
from workers_spawner.lifecycle_manager import LifecycleManager
from workers_spawner.redis_handlers import CommandHandler

logger = structlog.get_logger()

# Redis stream for incoming commands
COMMAND_STREAM = "cli-agent:commands"
RESPONSE_STREAM = "cli-agent:responses"
CONSUMER_GROUP = "workers-spawner"
CONSUMER_NAME = "worker-1"


async def ensure_stream_group(redis_client: redis.Redis) -> None:
    """Ensure the consumer group exists for the command stream."""
    try:
        await redis_client.xgroup_create(COMMAND_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("created_consumer_group", stream=COMMAND_STREAM, group=CONSUMER_GROUP)
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            # Group already exists
            pass
        else:
            raise


async def process_single_message(
    redis_client: redis.Redis,
    handler: CommandHandler,
    message_id: str | bytes,
    message_data: dict,
    semaphore: asyncio.Semaphore,
) -> None:
    """Process a single message with semaphore-based concurrency control."""
    message_id_str = message_id.decode() if isinstance(message_id, bytes) else message_id

    async with semaphore:
        try:
            # Parse command
            raw_data = message_data.get(b"data") or message_data.get("data")
            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode()

            command_data = json.loads(raw_data)
            command = command_data.get("command", "unknown")

            logger.debug(
                "processing_message",
                message_id=message_id_str,
                command=command,
            )

            # Handle command
            result = await handler.handle_message(command_data)

            # Publish response
            await redis_client.xadd(
                RESPONSE_STREAM,
                {"data": json.dumps(result)},
            )

            logger.info(
                "message_processed",
                message_id=message_id_str,
                command=command,
                success=result.get("success", False),
            )

        except Exception as e:
            logger.error(
                "message_processing_error",
                message_id=message_id_str,
                error=str(e),
            )
        finally:
            # Always acknowledge to prevent infinite retry loop
            await redis_client.xack(COMMAND_STREAM, CONSUMER_GROUP, message_id)


class MessageProcessor:
    """Manages concurrent message processing with graceful shutdown."""

    def __init__(
        self,
        redis_client: redis.Redis,
        handler: CommandHandler,
        max_concurrent: int,
    ):
        self.redis = redis_client
        self.handler = handler
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.pending_tasks: set[asyncio.Task] = set()
        self._running = False

    async def start(self) -> None:
        """Start processing messages."""
        self._running = True
        logger.info(
            "message_processor_started",
            max_concurrent=self.semaphore._value,
        )

        while self._running:
            try:
                # Read batch of messages (non-blocking allows checking _running flag)
                messages = await self.redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={COMMAND_STREAM: ">"},
                    count=10,  # Read up to 10 messages at once
                    block=1000,  # 1 second block for responsive shutdown
                )

                if not messages:
                    # Cleanup completed tasks periodically
                    self._cleanup_done_tasks()
                    continue

                for _stream_name, stream_messages in messages:
                    for message_id, message_data in stream_messages:
                        # Spawn task for each message (semaphore controls concurrency)
                        task = asyncio.create_task(
                            process_single_message(
                                self.redis,
                                self.handler,
                                message_id,
                                message_data,
                                self.semaphore,
                            )
                        )
                        self.pending_tasks.add(task)
                        task.add_done_callback(self.pending_tasks.discard)

                # Cleanup completed tasks
                self._cleanup_done_tasks()

            except asyncio.CancelledError:
                logger.info("message_processor_cancelled")
                break
            except Exception as e:
                logger.error("stream_read_error", error=str(e))
                await asyncio.sleep(1)

    def _cleanup_done_tasks(self) -> None:
        """Remove completed tasks from tracking set."""
        done_tasks = {t for t in self.pending_tasks if t.done()}
        for task in done_tasks:
            self.pending_tasks.discard(task)
            # Log any exceptions from completed tasks
            if task.exception():
                logger.error("task_exception", error=str(task.exception()))

    async def stop(self) -> None:
        """Stop processing and wait for pending tasks."""
        self._running = False
        if self.pending_tasks:
            logger.info("waiting_for_pending_tasks", count=len(self.pending_tasks))
            # Wait for all pending tasks with timeout
            done, pending = await asyncio.wait(
                self.pending_tasks,
                timeout=30,  # 30 second timeout for graceful shutdown
            )
            if pending:
                logger.warning("cancelling_pending_tasks", count=len(pending))
                for task in pending:
                    task.cancel()
        logger.info("message_processor_stopped")


async def main() -> None:
    """Main entrypoint."""
    setup_logging(service_name="workers_spawner")
    settings = get_settings()

    logger.info(
        "workers_spawner_starting",
        redis_url=settings.redis_url,
        max_concurrent=settings.max_concurrent_handlers,
    )

    # Initialize Redis
    redis_client = redis.from_url(settings.redis_url)

    # Initialize services
    container_service = ContainerService()
    event_publisher = EventPublisher(redis_client)
    lifecycle_manager = LifecycleManager(container_service, event_publisher)

    # Initialize command handler
    command_handler = CommandHandler(
        redis_client,
        container_service,
        event_publisher,
    )

    # Initialize message processor with concurrency control
    message_processor = MessageProcessor(
        redis_client,
        command_handler,
        max_concurrent=settings.max_concurrent_handlers,
    )

    # Ensure stream group exists
    await ensure_stream_group(redis_client)

    # Start lifecycle manager
    await lifecycle_manager.start()

    logger.info("workers_spawner_ready", stream=COMMAND_STREAM)

    try:
        # Process messages with parallel execution
        await message_processor.start()
    finally:
        await message_processor.stop()
        await lifecycle_manager.stop()
        await redis_client.close()


if __name__ == "__main__":
    asyncio.run(main())
