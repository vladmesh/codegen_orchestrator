"""Unit tests for SessionManager.

Tests session locking, state management, and lifecycle.
"""

from datetime import UTC, datetime
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.session_manager import SessionLock, SessionManager, SessionState


@pytest.fixture
def session_manager():
    """Create SessionManager instance."""
    return SessionManager()


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    with patch("src.session_manager.redis") as mock:
        mock_client = AsyncMock()
        mock.from_url.return_value = mock_client
        yield mock_client


class TestSessionLock:
    """Test SessionLock data class."""

    def test_to_dict(self):
        """Test serialization."""
        lock = SessionLock(
            thread_id="test_123",
            state=SessionState.PROCESSING,
            locked_at=datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC),
        )

        data = lock.to_dict()

        assert data["thread_id"] == "test_123"
        assert data["state"] == "processing"
        assert data["locked_at"] == "2024-01-15T10:00:00+00:00"

    def test_from_dict(self):
        """Test deserialization."""
        data = {
            "thread_id": "test_123",
            "state": "awaiting",
            "locked_at": "2024-01-15T10:00:00+00:00",
        }

        lock = SessionLock.from_dict(data)

        assert lock.thread_id == "test_123"
        assert lock.state == SessionState.AWAITING
        assert lock.locked_at == datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)


class TestSessionManager:
    """Test SessionManager functionality."""

    async def test_acquire_lock_success(self, session_manager, mock_redis):
        """Test successful lock acquisition."""
        mock_redis.set.return_value = True

        result = await session_manager.acquire_lock(123, "thread_1")

        assert result is True
        mock_redis.set.assert_called_once()
        call_args = mock_redis.set.call_args
        assert call_args.kwargs["nx"] is True  # SETNX
        assert call_args.kwargs["ex"] == 30 * 60  # 30 min TTL

    async def test_acquire_lock_already_locked(self, session_manager, mock_redis):
        """Test lock acquisition when already locked."""
        mock_redis.set.return_value = False

        result = await session_manager.acquire_lock(123, "thread_1")

        assert result is False

    async def test_get_session_exists(self, session_manager, mock_redis):
        """Test getting existing session."""
        lock_data = {
            "thread_id": "thread_1",
            "state": "processing",
            "locked_at": datetime.now(UTC).isoformat(),
        }
        mock_redis.get.return_value = json.dumps(lock_data)

        session = await session_manager.get_session(123)

        assert session is not None
        assert session.thread_id == "thread_1"
        assert session.state == SessionState.PROCESSING

    async def test_get_session_not_exists(self, session_manager, mock_redis):
        """Test getting non-existent session."""
        mock_redis.get.return_value = None

        session = await session_manager.get_session(123)

        assert session is None

    async def test_update_state(self, session_manager, mock_redis):
        """Test updating session state."""
        # Mock existing session
        lock_data = {
            "thread_id": "thread_1",
            "state": "processing",
            "locked_at": datetime.now(UTC).isoformat(),
        }
        mock_redis.get.return_value = json.dumps(lock_data)

        result = await session_manager.update_state(123, SessionState.AWAITING)

        assert result is True
        mock_redis.set.assert_called_once()
        # Verify state was updated
        call_data = json.loads(mock_redis.set.call_args[0][1])
        assert call_data["state"] == "awaiting"

    async def test_release_lock(self, session_manager, mock_redis):
        """Test releasing lock."""
        mock_redis.delete.return_value = 1

        result = await session_manager.release_lock(123)

        assert result is True
        mock_redis.delete.assert_called_once_with("session:lock:123")

    async def test_continue_session_awaiting(self, session_manager, mock_redis):
        """Test continuing session when awaiting."""
        lock_data = {
            "thread_id": "thread_1",
            "state": "awaiting",
            "locked_at": datetime.now(UTC).isoformat(),
        }
        mock_redis.get.return_value = json.dumps(lock_data)

        result = await session_manager.continue_session(123)

        assert result == "thread_1"
        # Should have updated state to PROCESSING
        mock_redis.set.assert_called_once()

    async def test_continue_session_processing(self, session_manager, mock_redis):
        """Test continuing session when processing (should fail)."""
        lock_data = {
            "thread_id": "thread_1",
            "state": "processing",
            "locked_at": datetime.now(UTC).isoformat(),
        }
        mock_redis.get.return_value = json.dumps(lock_data)

        result = await session_manager.continue_session(123)

        assert result is None  # Cannot continue

    async def test_is_locked_true(self, session_manager, mock_redis):
        """Test checking if session is locked."""
        lock_data = {
            "thread_id": "thread_1",
            "state": "processing",
            "locked_at": datetime.now(UTC).isoformat(),
        }
        mock_redis.get.return_value = json.dumps(lock_data)

        is_locked, state = await session_manager.is_locked(123)

        assert is_locked is True
        assert state == SessionState.PROCESSING

    async def test_is_locked_false(self, session_manager, mock_redis):
        """Test checking non-locked session."""
        mock_redis.get.return_value = None

        is_locked, state = await session_manager.is_locked(123)

        assert is_locked is False
        assert state is None

    @patch("src.session_manager.generate_thread_id")
    async def test_start_new_session(self, mock_generate_thread_id, session_manager, mock_redis):
        """Test starting a new session."""
        mock_generate_thread_id.return_value = "new_thread_123"
        mock_redis.delete.return_value = 1
        mock_redis.set.return_value = True

        thread_id = await session_manager.start_new_session(123)

        assert thread_id == "new_thread_123"
        # Should release old lock
        mock_redis.delete.assert_called_once()
        # Should acquire new lock
        mock_redis.set.assert_called_once()
