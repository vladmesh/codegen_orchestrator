"""Session management for agent conversations.

Simplified: only tracks Claude session IDs for conversation continuity.
Containers are ephemeral (created per request).
"""

from datetime import UTC, datetime

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()

# Redis key patterns
SESSION_KEY_PREFIX = "agent:session:"
SESSION_TTL_SECONDS = 86400 * 7  # 7 days


class SessionManager:
    """Manages Claude session IDs in Redis for conversation continuity."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    def _session_key(self, user_id: str) -> str:
        """Generate Redis key for user session."""
        return f"{SESSION_KEY_PREFIX}{user_id}"

    async def get_session_id(self, user_id: str) -> str | None:
        """Get Claude session ID for user.

        Args:
            user_id: User identifier

        Returns:
            Claude session ID if exists, None otherwise
        """
        key = self._session_key(user_id)
        data = await self.redis.hget(key, "claude_session_id")
        return data.decode() if data else None

    async def save_session_id(self, user_id: str, session_id: str) -> None:
        """Save Claude session ID for user.

        Args:
            user_id: User identifier
            session_id: Claude session ID
        """
        key = self._session_key(user_id)
        await self.redis.hset(
            key,
            mapping={
                "claude_session_id": session_id,
                "last_activity_at": datetime.now(UTC).isoformat(),
            },
        )
        await self.redis.expire(key, SESSION_TTL_SECONDS)

        logger.debug(
            "session_saved",
            user_id=user_id,
            session_id=session_id[:12] if session_id else None,
        )

    async def update_activity(self, user_id: str) -> None:
        """Update last activity timestamp for user.

        Args:
            user_id: User identifier
        """
        key = self._session_key(user_id)
        exists = await self.redis.exists(key)
        if exists:
            await self.redis.hset(key, "last_activity_at", datetime.now(UTC).isoformat())
            await self.redis.expire(key, SESSION_TTL_SECONDS)

    async def delete_session(self, user_id: str) -> None:
        """Delete session for user.

        Args:
            user_id: User identifier
        """
        key = self._session_key(user_id)
        await self.redis.delete(key)
        logger.info("session_deleted", user_id=user_id)
