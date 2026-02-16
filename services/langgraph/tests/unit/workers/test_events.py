"""Unit tests for publish_callback_event."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workers._events import publish_callback_event


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient."""
    client = MagicMock()
    client.redis = AsyncMock()
    return client


class TestPublishCallbackEvent:
    @pytest.mark.asyncio
    async def test_flat_format(self, mock_redis):
        """Event fields should be flat (not JSON-wrapped in 'data')."""
        await publish_callback_event(mock_redis, "test:stream", "completed", "task-1", "Done!")

        mock_redis.redis.xadd.assert_called_once()
        call_args = mock_redis.redis.xadd.call_args
        stream = call_args[0][0]
        fields = call_args[0][1]

        assert stream == "test:stream"
        assert fields["type"] == "system_event"
        assert fields["event"] == "completed"
        assert fields["task_id"] == "task-1"
        assert fields["text"] == "Done!"
        assert "timestamp" in fields
        # Verify NOT JSON-wrapped
        assert "data" not in fields

    @pytest.mark.asyncio
    async def test_with_user_id(self, mock_redis):
        """user_id should be included when provided."""
        await publish_callback_event(
            mock_redis,
            "test:stream",
            "progress",
            "task-1",
            "Working...",
            user_id="123",
        )

        fields = mock_redis.redis.xadd.call_args[0][1]
        assert fields["user_id"] == "123"

    @pytest.mark.asyncio
    async def test_with_project_id(self, mock_redis):
        """project_id should be included when provided."""
        await publish_callback_event(
            mock_redis,
            "test:stream",
            "progress",
            "task-1",
            "Working...",
            project_id="proj-abc",
        )

        fields = mock_redis.redis.xadd.call_args[0][1]
        assert fields["project_id"] == "proj-abc"

    @pytest.mark.asyncio
    async def test_without_callback_stream(self, mock_redis):
        """Should be a no-op when callback_stream is None."""
        await publish_callback_event(mock_redis, None, "completed", "task-1", "Done!")

        mock_redis.redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_type_field_always_system_event(self, mock_redis):
        """type field should always be 'system_event'."""
        for event_type in ("progress", "completed", "failed", "error"):
            mock_redis.redis.xadd.reset_mock()
            await publish_callback_event(mock_redis, "test:stream", event_type, "task-1", "msg")
            fields = mock_redis.redis.xadd.call_args[0][1]
            assert fields["type"] == "system_event"
            assert fields["event"] == event_type

    @pytest.mark.asyncio
    async def test_omits_empty_user_id(self, mock_redis):
        """Empty user_id should not be included in fields."""
        await publish_callback_event(
            mock_redis,
            "test:stream",
            "completed",
            "task-1",
            "Done!",
            user_id="",
        )

        fields = mock_redis.redis.xadd.call_args[0][1]
        assert "user_id" not in fields

    @pytest.mark.asyncio
    async def test_omits_empty_project_id(self, mock_redis):
        """Empty project_id should not be included in fields."""
        await publish_callback_event(
            mock_redis,
            "test:stream",
            "completed",
            "task-1",
            "Done!",
            project_id="",
        )

        fields = mock_redis.redis.xadd.call_args[0][1]
        assert "project_id" not in fields
