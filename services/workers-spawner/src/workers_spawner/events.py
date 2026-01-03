"""Event publishing for agent lifecycle and responses."""

import json
from typing import Any

import redis.asyncio as redis
import structlog

from workers_spawner.config import get_settings

logger = structlog.get_logger()


class EventPublisher:
    """Publishes agent events to Redis PubSub."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.settings = get_settings()

    async def publish_response(self, agent_id: str, response: str) -> None:
        """Publish agent response.

        Channel: agents:{agent_id}:response
        """
        channel = f"{self.settings.events_prefix}:{agent_id}:response"
        await self._publish(channel, {"type": "response", "data": response})

    async def publish_command_exit(self, agent_id: str, exit_code: int, output: str) -> None:
        """Publish command completion event.

        Channel: agents:{agent_id}:command_exit
        """
        channel = f"{self.settings.events_prefix}:{agent_id}:command_exit"
        await self._publish(
            channel, {"type": "command_exit", "exit_code": exit_code, "output": output}
        )

    async def publish_status(self, agent_id: str, state: str) -> None:
        """Publish agent status change.

        Channel: agents:{agent_id}:status
        """
        channel = f"{self.settings.events_prefix}:{agent_id}:status"
        await self._publish(channel, {"type": "status", "state": state})

    async def publish_message(self, agent_id: str, role: str, content: str) -> None:
        """Publish agent message event.

        Used for logging, analytics, and debugging agent conversations.

        Channel: agents:{agent_id}:message

        Args:
            agent_id: Agent container ID
            role: Message role ("user" or "assistant")
            content: Message content
        """
        channel = f"{self.settings.events_prefix}:{agent_id}:message"
        await self._publish(channel, {"type": "message", "role": role, "content": content})

    async def _publish(self, channel: str, data: dict[str, Any]) -> None:
        """Publish data to channel."""
        try:
            await self.redis.publish(channel, json.dumps(data))
            logger.debug("event_published", channel=channel, data_type=data.get("type"))
        except Exception as e:
            logger.error("event_publish_failed", channel=channel, error=str(e))
