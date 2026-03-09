"""Tests for story worker registry — Redis-backed worker_id per story."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.clients.story_worker_registry import (
    STORY_WORKERS_KEY,
    clear_story_worker,
    get_story_worker,
    set_story_worker,
)


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    return r


class TestStoryWorkerRegistry:
    """get/set/clear story worker mappings in Redis."""

    @pytest.mark.asyncio
    async def test_get_story_worker_returns_worker_id(self, mock_redis):
        """Returns worker_id when story has an active worker."""
        mock_redis.hget.return_value = b"dev-abc-12345678"

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-abc-12345678"
        mock_redis.hget.assert_called_once_with(STORY_WORKERS_KEY, "story-1")

    @pytest.mark.asyncio
    async def test_get_story_worker_returns_none_when_missing(self, mock_redis):
        """Returns None when no worker registered for story."""
        mock_redis.hget.return_value = None

        result = await get_story_worker(mock_redis, "story-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_story_worker_handles_str_response(self, mock_redis):
        """Handles Redis returning str instead of bytes."""
        mock_redis.hget.return_value = "dev-abc-12345678"

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-abc-12345678"

    @pytest.mark.asyncio
    async def test_set_story_worker(self, mock_redis):
        """Stores worker_id for story."""
        await set_story_worker(mock_redis, "story-1", "dev-abc-12345678")

        mock_redis.hset.assert_called_once_with(STORY_WORKERS_KEY, "story-1", "dev-abc-12345678")

    @pytest.mark.asyncio
    async def test_clear_story_worker(self, mock_redis):
        """Removes worker_id mapping for story."""
        await clear_story_worker(mock_redis, "story-1")

        mock_redis.hdel.assert_called_once_with(STORY_WORKERS_KEY, "story-1")
