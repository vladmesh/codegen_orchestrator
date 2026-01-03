"""Agent session state management."""

import json

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()


class AgentSessionManager:
    """Manages agent session context in Redis.

    Session context is agent-specific state (e.g., session_id for Claude)
    stored separately from container metadata.
    """

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    async def get_session_context(self, agent_id: str) -> dict | None:
        """Get session context for agent."""
        key = f"agent_session:{agent_id}"
        data = await self.redis.get(key)

        if not data:
            return None

        try:
            return json.loads(data)
        except json.JSONDecodeError:
            logger.warning("invalid_session_context", agent_id=agent_id)
            return None

    async def save_session_context(
        self,
        agent_id: str,
        context: dict,
        ttl_seconds: int = 7200,  # 2 hours default
    ) -> None:
        """Save session context for agent.

        Args:
            agent_id: Agent Container ID
            context: Dictionary of session data
            ttl_seconds: TTL for the redis key (default 2 hours)
        """
        key = f"agent_session:{agent_id}"
        data = json.dumps(context)

        await self.redis.set(key, data, ex=ttl_seconds)

        logger.debug(
            "session_context_saved",
            agent_id=agent_id,
            context_keys=list(context.keys()),
        )

    async def delete_session_context(self, agent_id: str) -> None:
        """Delete session context (on container deletion)."""
        key = f"agent_session:{agent_id}"
        await self.redis.delete(key)
