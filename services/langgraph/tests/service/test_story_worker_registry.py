"""Service tests for story worker registry — real Redis, no mocks."""

from __future__ import annotations

import pytest

from src.clients.story_worker_registry import (
    clear_story_worker,
    get_story_worker,
    set_story_worker,
)


class TestStoryWorkerRegistryReal:
    """Registry CRUD against real Redis."""

    @pytest.mark.asyncio
    async def test_set_and_get_worker(self, real_redis):
        """Store worker_id, retrieve it back."""
        await set_story_worker(real_redis, "story-svc-1", "dev-worker-abc")

        result = await get_story_worker(real_redis, "story-svc-1")
        assert result == "dev-worker-abc"

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self, real_redis):
        """Non-existent story returns None."""
        result = await get_story_worker(real_redis, "story-nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_clear_removes_worker(self, real_redis):
        """Clear removes the mapping, get returns None."""
        await set_story_worker(real_redis, "story-svc-2", "dev-worker-xyz")
        await clear_story_worker(real_redis, "story-svc-2")

        result = await get_story_worker(real_redis, "story-svc-2")
        assert result is None

    @pytest.mark.asyncio
    async def test_overwrite_worker(self, real_redis):
        """Setting worker twice overwrites the old value."""
        await set_story_worker(real_redis, "story-svc-3", "dev-old")
        await set_story_worker(real_redis, "story-svc-3", "dev-new")

        result = await get_story_worker(real_redis, "story-svc-3")
        assert result == "dev-new"

    @pytest.mark.asyncio
    async def test_multiple_stories_independent(self, real_redis):
        """Different stories have independent worker mappings."""
        await set_story_worker(real_redis, "story-a", "dev-1")
        await set_story_worker(real_redis, "story-b", "dev-2")

        assert await get_story_worker(real_redis, "story-a") == "dev-1"
        assert await get_story_worker(real_redis, "story-b") == "dev-2"

        await clear_story_worker(real_redis, "story-a")
        assert await get_story_worker(real_redis, "story-a") is None
        assert await get_story_worker(real_redis, "story-b") == "dev-2"

    @pytest.mark.asyncio
    async def test_cleanup_sends_delete_command_to_stream(self, real_redis):
        """Simulates _cleanup_story_worker flow: xadd to worker:commands stream."""
        # Set up a worker in registry
        await set_story_worker(real_redis, "story-cleanup", "dev-cleanup-abc")

        # Simulate what _cleanup_story_worker does: read, xadd, hdel
        worker_id = await get_story_worker(real_redis, "story-cleanup")
        assert worker_id == "dev-cleanup-abc"

        # Send delete command to stream (like scheduler does)
        import json

        cmd_data = json.dumps(
            {
                "command": "delete",
                "request_id": "cleanup-story-cleanup",
                "worker_id": worker_id,
                "reason": "completed",
            }
        )
        await real_redis.xadd("worker:commands", {"data": cmd_data})

        # Clear registry
        await clear_story_worker(real_redis, "story-cleanup")

        # Verify: worker gone from registry
        assert await get_story_worker(real_redis, "story-cleanup") is None

        # Verify: command is in the stream
        messages = await real_redis.xrange("worker:commands")
        assert len(messages) == 1
        payload = json.loads(messages[0][1][b"data"])
        assert payload["worker_id"] == "dev-cleanup-abc"
        assert payload["reason"] == "completed"
