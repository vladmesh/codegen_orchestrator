"""Agent Manager for Telegram Bot.

Manages the mapping between Telegram users and their Worker containers.
Provides a simple interface for sending messages to agents without
knowledge of agent-specific implementation details.
"""

import redis.asyncio as redis
import structlog

from src.clients.workers_spawner import workers_spawner
from src.config import get_settings

logger = structlog.get_logger(__name__)

# Redis key prefix for user->agent mapping
USER_AGENT_KEY_PREFIX = "telegram:user_agent:"


class AgentManager:
    """Manages user agent sessions."""

    def __init__(self) -> None:
        settings = get_settings()
        self.redis = redis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        """Close resources."""
        await self.redis.aclose()

    async def get_or_create_agent(self, user_id: int) -> str:
        """Get existing valid agent ID or create a new one.

        Args:
            user_id: Telegram user ID

        Returns:
            agent_id: Valid active agent ID
        """
        key = f"{USER_AGENT_KEY_PREFIX}{user_id}"

        # 1. Check if we have a mapped agent
        agent_id = await self.redis.get(key)

        if agent_id:
            # 2. Check if it's still alive/valid
            try:
                status = await workers_spawner.get_status(agent_id)
                if status and status.get("state") != "deleted":
                    # Valid agent found
                    logger.info("found_existing_agent", user_id=user_id, agent_id=agent_id)
                    return agent_id

                logger.info(
                    "existing_agent_invalid", user_id=user_id, agent_id=agent_id, status=status
                )
            except Exception as e:
                logger.warning("agent_status_check_failed", agent_id=agent_id, error=str(e))
                # Fall through to create new one

        # 3. Create new agent
        # For MVP/Dev: Always mount session volume to save cost/context
        # In production this might be conditional based on user tier
        mount_volume = True

        logger.info("creating_new_agent", user_id=user_id, mount_volume=mount_volume)

        try:
            agent_id = await workers_spawner.create_agent(
                str(user_id), mount_session_volume=mount_volume
            )

            # Save mapping (assume worker TTL matches logical expiry)
            # Worker default TTL is 2 hours. We keep mapping for longer?
            # If worker deletes itself, status check above will catch it.
            await self.redis.set(key, agent_id)

            logger.info("new_agent_created", user_id=user_id, agent_id=agent_id)
            return agent_id

        except Exception as e:
            logger.error("agent_creation_failed", user_id=user_id, error=str(e))
            raise

    async def send_message(self, user_id: int, message: str) -> str:
        """Send a message to the user's agent.

        This is a high-level interface that abstracts away agent-specific details.
        Session management, CLI commands, and response parsing are handled by
        workers-spawner and agent factories.

        Args:
            user_id: Telegram user ID
            message: User message text

        Returns:
            Agent's response text
        """
        agent_id = await self.get_or_create_agent(user_id)

        logger.info("sending_message_to_agent", user_id=user_id, agent_id=agent_id)

        try:
            result = await workers_spawner.send_message(agent_id, message, timeout=120)
            response = result["response"]

            logger.info(
                "agent_response_received",
                user_id=user_id,
                agent_id=agent_id,
                response_length=len(response),
            )

            return response

        except Exception as e:
            logger.error("send_message_failed", user_id=user_id, agent_id=agent_id, error=str(e))
            raise


# Singleton
agent_manager = AgentManager()
