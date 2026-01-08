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

# Redis key prefixes for bidirectional mapping
USER_AGENT_KEY_PREFIX = "telegram:user_agent:"
AGENT_USER_KEY_PREFIX = "telegram:agent_user:"


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

        # 3. Create new agent in persistent mode
        # For MVP/Dev: Always mount session volume to save cost/context
        mount_volume = True

        logger.info("creating_new_agent", user_id=user_id, mount_volume=mount_volume)

        try:
            agent_id = await workers_spawner.create_agent(
                str(user_id), mount_session_volume=mount_volume
            )

            # Save bidirectional mappings
            await self.redis.set(f"{USER_AGENT_KEY_PREFIX}{user_id}", agent_id)
            await self.redis.set(f"{AGENT_USER_KEY_PREFIX}{agent_id}", str(user_id))

            logger.info("new_agent_created", user_id=user_id, agent_id=agent_id)
            return agent_id

        except Exception as e:
            logger.error("agent_creation_failed", user_id=user_id, error=str(e))
            raise

    async def get_user_by_agent(self, agent_id: str) -> int | None:
        """Reverse lookup: get Telegram user ID for an agent.

        Args:
            agent_id: Agent container ID

        Returns:
            Telegram user ID or None if not found
        """
        key = f"{AGENT_USER_KEY_PREFIX}{agent_id}"
        user_id_str = await self.redis.get(key)
        return int(user_id_str) if user_id_str else None

    async def send_message(self, user_id: int, message: str) -> str:
        """Send a message to the user's agent and return response.

        This is now synchronous - response is returned directly,
        not via Redis stream (headless mode).

        If agent is not found (e.g. workers-spawner restarted), it will
        automatically clear the old mapping and create a new agent.

        Args:
            user_id: Telegram user ID
            message: User message text

        Returns:
            Agent's response text
        """
        agent_id = await self.get_or_create_agent(user_id)

        logger.info("sending_message_headless", user_id=user_id, agent_id=agent_id)

        try:
            result = await workers_spawner.send_message(agent_id, message, timeout=120)

            logger.info(
                "message_sent_and_received",
                user_id=user_id,
                agent_id=agent_id,
                response_length=len(result["response"]),
            )

            return result["response"]

        except RuntimeError as e:
            # Handle case when agent exists in Docker but not in workers-spawner memory
            # (happens after workers-spawner restart)
            if "not found" in str(e).lower():
                logger.warning(
                    "agent_not_in_spawner_memory",
                    user_id=user_id,
                    agent_id=agent_id,
                    error=str(e),
                )

                # Clear old mapping and retry with new agent
                await self.redis.delete(f"{USER_AGENT_KEY_PREFIX}{user_id}")
                await self.redis.delete(f"{AGENT_USER_KEY_PREFIX}{agent_id}")

                new_agent_id = await self.get_or_create_agent(user_id)
                logger.info("retry_with_new_agent", user_id=user_id, new_agent_id=new_agent_id)

                result = await workers_spawner.send_message(new_agent_id, message, timeout=120)
                return result["response"]

            raise

        except Exception as e:
            logger.error("send_message_failed", user_id=user_id, agent_id=agent_id, error=str(e))
            raise


# Singleton
agent_manager = AgentManager()
