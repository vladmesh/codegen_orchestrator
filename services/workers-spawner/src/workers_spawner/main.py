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


async def process_messages(
    redis_client: redis.Redis,
    handler: CommandHandler,
) -> None:
    """Process messages from the command stream."""
    response_stream = "cli-agent:responses"

    while True:
        try:
            # Read from stream with blocking
            messages = await redis_client.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={COMMAND_STREAM: ">"},
                count=1,
                block=5000,  # 5 second block
            )

            if not messages:
                continue

            for _stream_name, stream_messages in messages:
                for message_id, message_data in stream_messages:
                    try:
                        # Parse command
                        raw_data = message_data.get(b"data") or message_data.get("data")
                        if isinstance(raw_data, bytes):
                            raw_data = raw_data.decode()

                        command_data = json.loads(raw_data)

                        # Handle command
                        result = await handler.handle_message(command_data)

                        # Publish response
                        await redis_client.xadd(
                            response_stream,
                            {"data": json.dumps(result)},
                        )

                        # Acknowledge message
                        await redis_client.xack(COMMAND_STREAM, CONSUMER_GROUP, message_id)

                        logger.info(
                            "message_processed",
                            message_id=message_id.decode()
                            if isinstance(message_id, bytes)
                            else message_id,
                            success=result.get("success", False),
                        )

                    except Exception as e:
                        logger.error(
                            "message_processing_error",
                            message_id=message_id,
                            error=str(e),
                        )
                        # Still ack to prevent infinite retry loop
                        await redis_client.xack(COMMAND_STREAM, CONSUMER_GROUP, message_id)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("stream_read_error", error=str(e))
            await asyncio.sleep(1)


async def main() -> None:
    """Main entrypoint."""
    setup_logging(service_name="workers_spawner")
    settings = get_settings()

    logger.info("workers_spawner_starting", redis_url=settings.redis_url)

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

    # Ensure stream group exists
    await ensure_stream_group(redis_client)

    # Start lifecycle manager
    await lifecycle_manager.start()

    logger.info("workers_spawner_ready", stream=COMMAND_STREAM)

    try:
        # Process messages
        await process_messages(redis_client, command_handler)
    finally:
        await lifecycle_manager.stop()
        await redis_client.close()


if __name__ == "__main__":
    asyncio.run(main())
