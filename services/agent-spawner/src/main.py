"""Agent Spawner Service - Main entrypoint.

Listens to Redis for message requests and routes them to user-specific
CLI agent containers.
"""

import asyncio
from datetime import datetime
import json

import redis.asyncio as redis
import structlog

from shared.logging_config import setup_logging

from .config import get_settings
from .container_manager import ContainerManager
from .models import AgentSession, ContainerStatus, MessageRequest
from .session_manager import SessionManager

logger = structlog.get_logger()

# Redis channels
INCOMING_CHANNEL = "agent:incoming"  # PubSub for incoming messages
OUTGOING_PREFIX = "agent:outgoing:"  # Stream for outgoing messages


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

        # Get or create session
        session = await self.sessions.get_session(user_id)

        if not session:
            # Create new session and container
            session = await self._create_session(user_id)
        elif session.status == ContainerStatus.PAUSED:
            # Resume paused container
            await self._resume_session(session)
        elif session.status == ContainerStatus.DESTROYED:
            # Recreate destroyed container
            session = await self._create_session(user_id)

        # Ensure container is running
        if not session.container_id:
            logger.error("no_container_for_session", user_id=user_id)
            return "Ошибка: контейнер не найден. Попробуйте позже."

        # Execute in container
        result = await self.containers.execute(
            container_id=session.container_id,
            prompt=message,
            session_id=session.claude_session_id,
        )

        # Update session
        if result.session_id:
            await self.sessions.update_session_id(user_id, result.session_id)
        await self.sessions.update_activity(user_id)

        if not result.success:
            logger.error(
                "agent_execution_failed",
                user_id=user_id,
                error=result.error,
            )
            return f"Ошибка выполнения: {result.error or 'неизвестная ошибка'}"

        return result.output

    async def _create_session(self, user_id: str) -> AgentSession:
        """Create new session with container."""
        logger.info("creating_session", user_id=user_id)

        # Create container
        container_id = await self.containers.create_container(user_id)

        # Start container
        await self.containers.start_container(container_id)

        # Create session
        session = AgentSession(
            user_id=user_id,
            container_id=container_id,
            status=ContainerStatus.RUNNING,
        )
        await self.sessions.save_session(session)

        return session

    async def _resume_session(self, session: AgentSession) -> None:
        """Resume a paused session."""
        if not session.container_id:
            return

        logger.info("resuming_session", user_id=session.user_id)

        await self.containers.resume_container(session.container_id)
        await self.sessions.update_status(session.user_id, ContainerStatus.RUNNING)

    async def cleanup_idle_containers(self) -> None:
        """Cleanup idle containers based on timeouts."""
        settings = self.settings

        # Find containers to pause (idle > 5 min)
        idle_sessions = await self.sessions.get_idle_sessions(settings.container_idle_timeout_sec)

        for session in idle_sessions:
            if session.status == ContainerStatus.RUNNING and session.container_id:
                try:
                    await self.containers.pause_container(session.container_id)
                    await self.sessions.update_status(session.user_id, ContainerStatus.PAUSED)
                    logger.info(
                        "container_auto_paused",
                        user_id=session.user_id,
                    )
                except Exception as e:
                    logger.error(
                        "container_pause_failed",
                        user_id=session.user_id,
                        error=str(e),
                    )

        # Find containers to destroy (idle > 24h)
        destroy_sessions = await self.sessions.get_idle_sessions(
            settings.container_destroy_timeout_sec
        )

        for session in destroy_sessions:
            if session.container_id:
                try:
                    await self.containers.destroy_container(session.container_id)
                    await self.sessions.update_status(session.user_id, ContainerStatus.DESTROYED)
                    logger.info(
                        "container_auto_destroyed",
                        user_id=session.user_id,
                    )
                except Exception as e:
                    logger.error(
                        "container_destroy_failed",
                        user_id=session.user_id,
                        error=str(e),
                    )


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

        # Publish response to user's outgoing stream
        await redis_client.xadd(
            f"{OUTGOING_PREFIX}{request.user_id}",
            {
                "response": response,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )

        logger.info(
            "response_published",
            user_id=request.user_id,
            response_length=len(response),
        )

    except Exception as e:
        logger.error(
            "request_handling_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


async def cleanup_loop(spawner: AgentSpawner) -> None:
    """Periodic cleanup of idle containers."""
    while True:
        try:
            await spawner.cleanup_idle_containers()
        except Exception as e:
            logger.error("cleanup_failed", error=str(e))

        # Run cleanup every minute
        await asyncio.sleep(60)


async def main() -> None:
    """Main entrypoint."""
    setup_logging(service_name="agent_spawner")
    settings = get_settings()

    logger.info("agent_spawner_starting", redis_url=settings.redis_url)

    redis_client = redis.from_url(settings.redis_url)
    container_manager = ContainerManager()
    session_manager = SessionManager(redis_client)

    spawner = AgentSpawner(redis_client, container_manager, session_manager)

    # Start cleanup task
    cleanup_task = asyncio.create_task(cleanup_loop(spawner))

    # Subscribe to incoming messages
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(INCOMING_CHANNEL)

    logger.info("agent_spawner_ready", channel=INCOMING_CHANNEL)

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                asyncio.create_task(handle_request(spawner, redis_client, message))
    finally:
        cleanup_task.cancel()
        await pubsub.unsubscribe(INCOMING_CHANNEL)
        close_result = pubsub.close()
        if asyncio.iscoroutine(close_result):
            await close_result


if __name__ == "__main__":
    asyncio.run(main())
