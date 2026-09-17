"""Tests for story worker registry — Redis-backed worker_id per story."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from shared.queues import WORKER_COMMANDS
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
        mock_redis.hget.side_effect = [b"dev-abc-12345678", b"RUNNING"]

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-abc-12345678"
        assert mock_redis.hget.await_args_list == [
            call(STORY_WORKERS_KEY, "story-1"),
            call("worker:status:dev-abc-12345678", "status"),
        ]

    @pytest.mark.asyncio
    async def test_get_story_worker_returns_none_when_missing(self, mock_redis):
        """Returns None when no worker registered for story."""
        mock_redis.hget.return_value = None

        result = await get_story_worker(mock_redis, "story-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_story_worker_handles_str_response(self, mock_redis):
        """Handles Redis returning str instead of bytes."""
        mock_redis.hget.side_effect = ["dev-abc-12345678", "RUNNING"]

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-abc-12345678"

    @pytest.mark.asyncio
    async def test_terminal_worker_is_evicted_before_replacement(self, mock_redis):
        """A dead registry entry is cleaned and its workspace released."""
        mock_redis.hget.side_effect = [b"dev-dead", b"DEAD"]
        mock_redis.hgetall.return_value = {b"project_id": b"project-1"}
        mock_redis.eval.return_value = 1
        mock_redis.get.return_value = None

        result = await get_story_worker(mock_redis, "story-1")

        assert result is None
        mock_redis.eval.assert_awaited_once()
        mock_redis.xadd.assert_awaited_once()
        stream, fields = mock_redis.xadd.await_args.args
        assert stream == WORKER_COMMANDS
        assert '"worker_id":"dev-dead"' in fields["data"]
        mock_redis.get.assert_awaited_once_with("workspace:lock:project-1")

    @pytest.mark.asyncio
    async def test_replacement_mapping_wins_cleanup_race(self, mock_redis):
        """Compare-and-delete never removes a concurrently registered worker."""
        mock_redis.hget.side_effect = [b"dev-dead", b"DEAD", b"dev-new", b"RUNNING"]
        mock_redis.hgetall.return_value = {b"project_id": b"project-1"}
        mock_redis.eval.return_value = 0

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-new"
        mock_redis.xadd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deleted_worker_binding_is_evicted(self, mock_redis):
        """No status and no meta is a removed worker, not an inconclusive read."""
        mock_redis.hget.side_effect = [b"dev-deleted", None]
        mock_redis.hgetall.return_value = {}
        mock_redis.eval.return_value = 1

        result = await get_story_worker(mock_redis, "story-1")

        assert result is None
        mock_redis.eval.assert_awaited_once()
        # Nothing is left to delete, so teardown is not asked for a second time.
        mock_redis.xadd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_worker_with_meta_but_no_status_is_still_returned(self, mock_redis):
        """A worker mid-creation has its metadata but not yet a status."""
        mock_redis.hget.side_effect = [b"dev-starting", None]
        mock_redis.hgetall.return_value = {b"project_id": b"project-1"}

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-starting"
        mock_redis.eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unrecognised_status_stays_inconclusive(self, mock_redis):
        """A status this version cannot read is not proof the worker is gone."""
        mock_redis.hget.side_effect = [b"dev-unknown", b"SOMETHING_NEW"]

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-unknown"
        mock_redis.eval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replacement_wins_dead_binding_eviction_race(self, mock_redis):
        """Compare-and-delete never removes a binding a newer worker just took."""
        mock_redis.hget.side_effect = [b"dev-deleted", None, b"dev-new", b"RUNNING"]
        mock_redis.hgetall.return_value = {}
        mock_redis.eval.return_value = 0

        result = await get_story_worker(mock_redis, "story-1")

        assert result == "dev-new"

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
