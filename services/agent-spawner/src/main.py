"""Agent Spawner Service - Main entrypoint.

Listens to Redis for message requests and routes them to user-specific
CLI agent containers.
"""

import asyncio
from datetime import UTC, datetime
import json

import redis.asyncio as redis
import structlog

from shared.logging_config import setup_logging

from .config import get_settings
from .container_manager import ContainerManager
from .models import MessageRequest
from .session_manager import SessionManager

logger = structlog.get_logger()

# Redis channels
INCOMING_CHANNEL = "agent:incoming"  # PubSub for incoming messages
OUTGOING_STREAM = "agent:outgoing"  # Stream for outgoing messages


class AgentSpawner:
    """Orchestrates message routing to per-user agent containers."""

    def __init__(
        self,
        redis_client: redis.Redis,
        container_manager: ContainerManager,
        session_manager: SessionManager,
    ) -> None:
        self.redis = redis_client
        self.containers = container_manager
        self.sessions = session_manager
        self.settings = get_settings()

    async def handle_message(self, user_id: str, message: str) -> str:
        """Handle incoming message for user.

        Args:
            user_id: User identifier
            message: Message content

        Returns:
            Agent response
        """
        logger.info(
            "message_received",
            user_id=user_id,
            message_length=len(message),
        )

        # Get existing session for conversation continuity
        session_id = await self.sessions.get_session_id(user_id)

        # Execute in ephemeral container
        result = await self.containers.execute(
            user_id=user_id,
            prompt=message,
            session_id=session_id,
        )

        # Update session ID for conversation continuity
        if result.session_id:
            await self.sessions.save_session_id(user_id, result.session_id)

        await self.sessions.update_activity(user_id)

        if not result.success:
            logger.error(
                "agent_execution_failed",
                user_id=user_id,
                error=result.error,
                exit_code=result.exit_code,
            )
            return f"Ошибка выполнения: {result.error or 'неизвестная ошибка'}"

        return result.output


async def handle_request(
    spawner: AgentSpawner,
    redis_client: redis.Redis,
    message: dict,
) -> None:
    """Handle a message request from Redis."""
    try:
        data = json.loads(message["data"])
        request = MessageRequest(**data)

        response = await spawner.handle_message(request.user_id, request.message)

        # Publish response to user's outgoing stream in Telegram-compatible format
        response_data = {
            "chat_id": request.chat_id,
            "text": response,
            "reply_to_message_id": request.message_id,
            "user_id": request.user_id,
            "correlation_id": request.correlation_id,
            "timestamp": datetime.now(UTC).isoformat(),
        }

        # Wrap in "data" field as JSON string (expected by RedisStreamClient consumer)
        await redis_client.xadd(
            OUTGOING_STREAM,
            {"data": json.dumps(response_data)},
        )

        logger.info(
            "response_published",
            user_id=request.user_id,
            response_length=len(response),
            correlation_id=request.correlation_id,
        )

    except Exception as e:
        logger.error(
            "request_handling_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


async def main() -> None:
    """Main entrypoint."""
    setup_logging(service_name="agent_spawner")
    settings = get_settings()

    logger.info("agent_spawner_starting", redis_url=settings.redis_url)

    redis_client = redis.from_url(settings.redis_url)
    container_manager = ContainerManager()
    session_manager = SessionManager(redis_client)

    spawner = AgentSpawner(redis_client, container_manager, session_manager)

    # Subscribe to incoming messages
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(INCOMING_CHANNEL)

    logger.info("agent_spawner_ready", channel=INCOMING_CHANNEL)

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                asyncio.create_task(handle_request(spawner, redis_client, message))
    finally:
        await pubsub.unsubscribe(INCOMING_CHANNEL)
        close_result = pubsub.close()
        if asyncio.iscoroutine(close_result):
            await close_result


if __name__ == "__main__":
    asyncio.run(main())
