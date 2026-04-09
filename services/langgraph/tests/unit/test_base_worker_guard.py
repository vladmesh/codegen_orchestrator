"""Tests for consumer-side staleness guard in _base.py."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.story import StoryStatus


@pytest.fixture()
def mock_api_client():
    """Mock the langgraph API client."""
    with patch("src.consumers._base.api_client") as mock:
        mock.get = AsyncMock()
        mock.get_story = AsyncMock()
        yield mock


@pytest.fixture()
def mock_redis():
    """Mock RedisStreamClient."""
    redis = AsyncMock()
    redis.connect = AsyncMock()
    redis.close = AsyncMock()
    redis.ack = AsyncMock()
    redis.consume = AsyncMock()
    redis.ensure_consumer_group = AsyncMock()
    return redis


class TestCheckMessageStaleness:
    """Tests for _check_message_staleness helper."""

    @pytest.mark.asyncio()
    async def test_stale_run_detected(self, mock_api_client):
        """Message with task_id pointing to COMPLETED run → stale."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get.return_value = {"status": RunStatus.COMPLETED.value}

        result = await _check_message_staleness({"task_id": "eng-abc123"})
        assert result is True
        mock_api_client.get.assert_called_once_with("runs/eng-abc123")

    @pytest.mark.asyncio()
    async def test_stale_run_failed(self, mock_api_client):
        """Message with task_id pointing to FAILED run → stale."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get.return_value = {"status": RunStatus.FAILED.value}

        result = await _check_message_staleness({"task_id": "deploy-xyz"})
        assert result is True

    @pytest.mark.asyncio()
    async def test_stale_run_cancelled(self, mock_api_client):
        """Message with task_id pointing to CANCELLED run → stale."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get.return_value = {"status": RunStatus.CANCELLED.value}

        result = await _check_message_staleness({"task_id": "qa-001"})
        assert result is True

    @pytest.mark.asyncio()
    async def test_fresh_run_queued(self, mock_api_client):
        """Message with task_id pointing to QUEUED run → fresh."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get.return_value = {"status": RunStatus.QUEUED.value}

        result = await _check_message_staleness({"task_id": "eng-fresh"})
        assert result is False

    @pytest.mark.asyncio()
    async def test_fresh_run_running(self, mock_api_client):
        """Message with task_id pointing to RUNNING run → fresh."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get.return_value = {"status": RunStatus.RUNNING.value}

        result = await _check_message_staleness({"task_id": "eng-active"})
        assert result is False

    @pytest.mark.asyncio()
    async def test_missing_task_id_skips_guard(self, mock_api_client):
        """Message without task_id → not stale (guard skipped)."""
        from src.consumers._base import _check_message_staleness

        result = await _check_message_staleness({"project_id": "proj-1"})
        assert result is False
        mock_api_client.get.assert_not_called()

    @pytest.mark.asyncio()
    async def test_story_id_stale_completed(self, mock_api_client):
        """Message with story_id (no task_id) pointing to COMPLETED story → stale."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get_story.return_value = MagicMock(status=StoryStatus.COMPLETED.value)

        result = await _check_message_staleness({"story_id": "story-done"})
        assert result is True
        mock_api_client.get_story.assert_called_once_with("story-done")

    @pytest.mark.asyncio()
    async def test_story_id_stale_failed(self, mock_api_client):
        """Message with story_id (no task_id) pointing to FAILED story → stale."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get_story.return_value = MagicMock(status=StoryStatus.FAILED.value)

        result = await _check_message_staleness({"story_id": "story-fail"})
        assert result is True

    @pytest.mark.asyncio()
    async def test_story_id_stale_archived(self, mock_api_client):
        """Message with story_id (no task_id) pointing to ARCHIVED story → stale."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get_story.return_value = MagicMock(status=StoryStatus.ARCHIVED.value)

        result = await _check_message_staleness({"story_id": "story-archived"})
        assert result is True

    @pytest.mark.asyncio()
    async def test_story_id_fresh_created(self, mock_api_client):
        """Message with story_id pointing to CREATED story → fresh."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get_story.return_value = MagicMock(status=StoryStatus.CREATED.value)

        result = await _check_message_staleness({"story_id": "story-new"})
        assert result is False

    @pytest.mark.asyncio()
    async def test_guard_api_failure_proceeds(self, mock_api_client):
        """API failure during guard → not stale (proceed with processing)."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=HTTPStatus.NOT_FOUND),
        )

        result = await _check_message_staleness({"task_id": "eng-broken"})
        assert result is False

    @pytest.mark.asyncio()
    async def test_guard_story_api_failure_proceeds(self, mock_api_client):
        """API failure during story guard → not stale (proceed with processing)."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get_story.side_effect = Exception("connection error")

        result = await _check_message_staleness({"story_id": "story-broken"})
        assert result is False

    @pytest.mark.asyncio()
    async def test_task_id_takes_precedence_over_story_id(self, mock_api_client):
        """When both task_id and story_id present, check task_id (run) not story."""
        from src.consumers._base import _check_message_staleness

        mock_api_client.get.return_value = {"status": RunStatus.QUEUED.value}

        result = await _check_message_staleness({"task_id": "eng-123", "story_id": "story-456"})
        assert result is False
        mock_api_client.get.assert_called_once_with("runs/eng-123")
        mock_api_client.get_story.assert_not_called()
