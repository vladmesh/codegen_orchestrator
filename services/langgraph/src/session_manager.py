"""Session lifecycle management for Dynamic PO.

Handles:
- Session locking (prevents concurrent processing)
- Awaiting user response state
- Session timeout (30 minutes via Redis TTL)
- Thread ID lifecycle
"""

from datetime import UTC, datetime
from enum import Enum
import json

import redis.asyncio as redis
import structlog

from .config.settings import get_settings
from .thread_manager import generate_thread_id

logger = structlog.get_logger()

SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 minutes


class SessionState(str, Enum):
    """Session states."""

    PROCESSING = "processing"  # Graph is running
    AWAITING = "awaiting"  # Waiting for user response
    IDLE = "idle"  # No active session


class SessionLock:
    """Session lock data."""

    def __init__(
        self,
        thread_id: str,
        state: SessionState,
        locked_at: datetime,
    ):
        self.thread_id = thread_id
        self.state = state
        self.locked_at = locked_at

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "state": self.state.value,
            "locked_at": self.locked_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionLock":
        return cls(
            thread_id=data["thread_id"],
            state=SessionState(data["state"]),
            locked_at=datetime.fromisoformat(data["locked_at"]),
        )


class SessionManager:
    """Manages user session lifecycle."""

    def __init__(self):
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            settings = get_settings()
            self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        return self._redis

    def _lock_key(self, user_id: int) -> str:
        return f"session:lock:{user_id}"

    async def get_session(self, user_id: int) -> SessionLock | None:
        """Get current session lock if exists."""
        r = await self._get_redis()
        data = await r.get(self._lock_key(user_id))
        if not data:
            return None
        return SessionLock.from_dict(json.loads(data))

    async def acquire_lock(
        self,
        user_id: int,
        thread_id: str,
        state: SessionState = SessionState.PROCESSING,
    ) -> bool:
        """Try to acquire session lock.

        Returns True if lock acquired, False if already locked.
        """
        r = await self._get_redis()
        key = self._lock_key(user_id)

        lock = SessionLock(
            thread_id=thread_id,
            state=state,
            locked_at=datetime.now(UTC),
        )

        # SETNX with TTL
        acquired = await r.set(
            key,
            json.dumps(lock.to_dict()),
            nx=True,  # Only set if not exists
            ex=SESSION_TIMEOUT_SECONDS,
        )

        if acquired:
            logger.debug("session_lock_acquired", user_id=user_id, thread_id=thread_id)

        return bool(acquired)

    async def update_state(
        self,
        user_id: int,
        state: SessionState,
    ) -> bool:
        """Update session state (e.g., PROCESSING -> AWAITING)."""
        r = await self._get_redis()
        key = self._lock_key(user_id)

        current = await self.get_session(user_id)
        if not current:
            return False

        current.state = state
        await r.set(
            key,
            json.dumps(current.to_dict()),
            ex=SESSION_TIMEOUT_SECONDS,  # Refresh TTL
        )

        logger.debug(
            "session_state_updated",
            user_id=user_id,
            thread_id=current.thread_id,
            state=state.value,
        )
        return True

    async def release_lock(self, user_id: int) -> bool:
        """Release session lock (on task completion)."""
        r = await self._get_redis()
        key = self._lock_key(user_id)

        deleted = await r.delete(key)

        if deleted:
            logger.debug("session_lock_released", user_id=user_id)

        return bool(deleted)

    async def refresh_timeout(self, user_id: int) -> bool:
        """Refresh session timeout (on new user message)."""
        r = await self._get_redis()
        key = self._lock_key(user_id)
        return bool(await r.expire(key, SESSION_TIMEOUT_SECONDS))

    async def start_new_session(self, user_id: int) -> str:
        """Start a new session: release old, generate new thread_id, acquire lock.

        Returns new thread_id.
        """
        # Release any existing lock
        await self.release_lock(user_id)

        # Generate new thread_id
        thread_id = await generate_thread_id(user_id)

        # Acquire lock
        await self.acquire_lock(user_id, thread_id)

        logger.info("new_session_started", user_id=user_id, thread_id=thread_id)
        return thread_id

    async def continue_session(self, user_id: int) -> str | None:
        """Continue existing session if awaiting user response.

        Returns thread_id if can continue, None if should start new.
        """
        session = await self.get_session(user_id)

        if not session:
            return None

        if session.state == SessionState.AWAITING:
            # Update state to processing and refresh timeout
            await self.update_state(user_id, SessionState.PROCESSING)
            await self.refresh_timeout(user_id)
            logger.debug(
                "session_continued",
                user_id=user_id,
                thread_id=session.thread_id,
            )
            return session.thread_id

        # Session is PROCESSING - cannot continue
        return None

    async def is_locked(self, user_id: int) -> tuple[bool, SessionState | None]:
        """Check if session is locked.

        Returns (is_locked, state).
        """
        session = await self.get_session(user_id)
        if not session:
            return False, None
        return True, session.state

    async def close(self) -> None:
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()


# Global instance
session_manager = SessionManager()
