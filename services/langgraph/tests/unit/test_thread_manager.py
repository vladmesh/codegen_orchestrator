"""Unit tests for Thread ID management."""

from unittest.mock import AsyncMock, patch

import pytest


class TestThreadManager:
    """Tests for thread_manager module."""

    @pytest.mark.asyncio
    async def test_generate_thread_id_format(self):
        """Test that generate_thread_id returns correct format."""
        mock_redis = AsyncMock()
        mock_redis.incr.return_value = 42

        with patch("src.thread_manager._redis_client", mock_redis):
            from src.thread_manager import generate_thread_id

            result = await generate_thread_id(625038902)

            assert result == "user_625038902_42"
            mock_redis.incr.assert_called_once_with("thread:sequence:625038902")

    @pytest.mark.asyncio
    async def test_get_current_thread_id_exists(self):
        """Test get_current_thread_id when thread exists."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = "43"

        with patch("src.thread_manager._redis_client", mock_redis):
            from src.thread_manager import get_current_thread_id

            result = await get_current_thread_id(625038902)

            assert result == "user_625038902_43"
            mock_redis.get.assert_called_once_with("thread:sequence:625038902")

    @pytest.mark.asyncio
    async def test_get_current_thread_id_not_exists(self):
        """Test get_current_thread_id when no thread exists."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None

        with patch("src.thread_manager._redis_client", mock_redis):
            from src.thread_manager import get_current_thread_id

            result = await get_current_thread_id(625038902)

            assert result is None

    @pytest.mark.asyncio
    async def test_get_or_create_thread_id_creates_new(self):
        """Test get_or_create_thread_id creates new thread if none exists."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None
        mock_redis.incr.return_value = 1

        with patch("src.thread_manager._redis_client", mock_redis):
            from src.thread_manager import get_or_create_thread_id

            result = await get_or_create_thread_id(625038902)

            assert result == "user_625038902_1"
            mock_redis.get.assert_called_once()
            mock_redis.incr.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_or_create_thread_id_returns_existing(self):
        """Test get_or_create_thread_id returns existing thread."""
        mock_redis = AsyncMock()
        mock_redis.get.return_value = "5"

        with patch("src.thread_manager._redis_client", mock_redis):
            from src.thread_manager import get_or_create_thread_id

            result = await get_or_create_thread_id(625038902)

            assert result == "user_625038902_5"
            mock_redis.get.assert_called_once()
            mock_redis.incr.assert_not_called()
