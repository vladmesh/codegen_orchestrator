from fakeredis import FakeAsyncRedis
import pytest
from worker_wrapper.session import SessionManager


@pytest.fixture
def fake_redis():
    return FakeAsyncRedis()


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_creates_new_session_if_not_exists(self, fake_redis):
        """Should generate new session_id if none in Redis."""
        manager = SessionManager(fake_redis, worker_id="worker-1")

        session_id = await manager.get_or_create_session()

        assert session_id is not None
        uuid_length = 36
        assert len(session_id) == uuid_length  # UUID format

        # Check it's saved
        saved = await fake_redis.get("worker:session:worker-1")
        assert saved.decode() == session_id

    @pytest.mark.asyncio
    async def test_returns_existing_session(self, fake_redis):
        """Should return existing session_id from Redis."""
        await fake_redis.set("worker:session:worker-1", "existing-123")

        manager = SessionManager(fake_redis, worker_id="worker-1")
        session_id = await manager.get_or_create_session()

        assert session_id == "existing-123"

    @pytest.mark.asyncio
    async def test_session_has_ttl(self, fake_redis):
        """Session should have TTL set."""
        ttl_seconds = 7200
        manager = SessionManager(fake_redis, worker_id="worker-1", ttl_seconds=ttl_seconds)

        await manager.get_or_create_session()

        ttl = await fake_redis.ttl("worker:session:worker-1")
        assert 0 < ttl <= ttl_seconds

    @pytest.mark.asyncio
    async def test_updates_session_on_each_access(self, fake_redis):
        """Session TTL should refresh on access."""
        ttl_seconds = 7200
        manager = SessionManager(fake_redis, worker_id="worker-1", ttl_seconds=ttl_seconds)

        await manager.get_or_create_session()

        # Manually lower TTL
        expired_ttl = 100
        await fake_redis.expire("worker:session:worker-1", expired_ttl)

        # Access again
        await manager.get_or_create_session()

        # TTL should be refreshed
        ttl = await fake_redis.ttl("worker:session:worker-1")
        assert ttl > expired_ttl

    @pytest.mark.asyncio
    async def test_clear_session_deletes_key(self, fake_redis):
        """clear_session should delete the session key from Redis."""
        manager = SessionManager(fake_redis, worker_id="worker-1")
        await manager.get_or_create_session()

        # Verify session exists
        assert await fake_redis.get("worker:session:worker-1") is not None

        # Clear it
        await manager.clear_session()

        # Should be gone
        assert await fake_redis.get("worker:session:worker-1") is None

    @pytest.mark.asyncio
    async def test_get_session_after_clear_returns_none_for_claude(self, fake_redis):
        """After clear, get_or_create with create_new=False returns None (Claude behavior)."""
        manager = SessionManager(fake_redis, worker_id="worker-1")

        # Simulate: session was captured from previous run
        await manager.update_session("previous-session-123")

        # Clear for retry
        await manager.clear_session()

        # Claude mode: create_new=False → should return None
        session_id = await manager.get_or_create_session(create_new=False)
        assert session_id is None
