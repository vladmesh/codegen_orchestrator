"""Session management for agent containers."""

from datetime import datetime
import json

import redis.asyncio as redis
import structlog

from .models import AgentSession, ContainerStatus

logger = structlog.get_logger()

# Redis key patterns
SESSION_KEY_PREFIX = "agent:session:"
SESSION_TTL_SECONDS = 86400 * 7  # 7 days


class SessionManager:
    """Manages agent sessions in Redis."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    def _session_key(self, user_id: str) -> str:
        """Generate Redis key for user session."""
        return f"{SESSION_KEY_PREFIX}{user_id}"

    async def get_session(self, user_id: str) -> AgentSession | None:
        """Get session for user.

        Args:
            user_id: User identifier

        Returns:
            AgentSession if exists, None otherwise
        """
        key = self._session_key(user_id)
        data = await self.redis.get(key)

        if not data:
            return None

        try:
            parsed = json.loads(data)
            return AgentSession.from_dict(parsed)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "session_parse_failed",
                user_id=user_id,
                error=str(e),
            )
            return None

    async def save_session(self, session: AgentSession) -> None:
        """Save session to Redis.

        Args:
            session: Session to save
        """
        key = self._session_key(session.user_id)
        data = json.dumps(session.to_dict())

        await self.redis.setex(
            key,
            SESSION_TTL_SECONDS,
            data,
        )

        logger.debug(
            "session_saved",
            user_id=session.user_id,
            status=session.status.value,
        )

    async def update_activity(self, user_id: str) -> None:
        """Update last activity timestamp for user.

        Args:
            user_id: User identifier
        """
        session = await self.get_session(user_id)
        if session:
            session.last_activity_at = datetime.utcnow()
            await self.save_session(session)

    async def update_session_id(self, user_id: str, session_id: str) -> None:
        """Update Claude session ID for user.

        Args:
            user_id: User identifier
            session_id: New Claude session ID
        """
        session = await self.get_session(user_id)
        if session:
            session.claude_session_id = session_id
            session.last_activity_at = datetime.utcnow()
            await self.save_session(session)

    async def update_status(self, user_id: str, status: ContainerStatus) -> None:
        """Update container status for user.

        Args:
            user_id: User identifier
            status: New container status
        """
        session = await self.get_session(user_id)
        if session:
            session.status = status
            await self.save_session(session)

    async def delete_session(self, user_id: str) -> None:
        """Delete session for user.

        Args:
            user_id: User identifier
        """
        key = self._session_key(user_id)
        await self.redis.delete(key)
        logger.info("session_deleted", user_id=user_id)

    async def get_idle_sessions(self, idle_seconds: int) -> list[AgentSession]:
        """Get sessions that have been idle for specified time.

        Args:
            idle_seconds: Minimum idle time in seconds

        Returns:
            List of idle sessions
        """
        idle_sessions = []
        cutoff = datetime.utcnow()

        # Scan for all session keys
        async for key in self.redis.scan_iter(f"{SESSION_KEY_PREFIX}*"):
            data = await self.redis.get(key)
            if not data:
                continue

            try:
                parsed = json.loads(data)
                session = AgentSession.from_dict(parsed)

                # Check if idle
                idle_time = (cutoff - session.last_activity_at).total_seconds()
                if idle_time >= idle_seconds:
                    idle_sessions.append(session)

            except (json.JSONDecodeError, KeyError):
                continue

        return idle_sessions
