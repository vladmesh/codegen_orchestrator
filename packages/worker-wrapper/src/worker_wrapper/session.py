from typing import Any
import uuid


# Define Protocol for Redis client to support both real redis.asyncio.Redis and FakeAsyncRedis
class AsyncRedisClient:
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any, nx: bool = False) -> Any: ...
    async def expire(self, key: str, time: int) -> Any: ...
    async def ttl(self, key: str) -> int: ...


class SessionManager:
    """Manages persistent sessions for workers."""

    def __init__(self, redis: AsyncRedisClient, worker_id: str, ttl_seconds: int = 3600):
        self.redis = redis
        self.worker_id = worker_id
        self.ttl = ttl_seconds
        self._key = f"worker:session:{worker_id}"

    async def get_or_create_session(self) -> str:
        """
        Get existing session ID or create a new one.
        Refreshes TTL on access.
        """
        # Try to get existing
        raw_session_id = await self.redis.get(self._key)

        if raw_session_id:
            session_id = raw_session_id
            if isinstance(session_id, bytes):
                session_id = session_id.decode()
        else:
            # Create new
            session_id = str(uuid.uuid4())
            # Use set with nx=True to avoid race conditions (first write wins)
            # In practice, worker_id is unique per container, so race is unlikely but good practice
            created = await self.redis.set(self._key, session_id, nx=True)
            if not created:
                # Race condition: someone else created it, fetch again
                raw_session_id = await self.redis.get(self._key)
                if raw_session_id:
                    session_id = raw_session_id
                    if isinstance(session_id, bytes):
                        session_id = session_id.decode()

        # Refresh TTL
        await self.redis.expire(self._key, self.ttl)

        return session_id
