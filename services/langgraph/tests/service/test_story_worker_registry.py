"""Service tests for story worker registry — real Redis, no mocks."""

from __future__ import annotations

import pytest

from src.clients.story_worker_registry import (
    clear_story_worker,
    get_story_worker,
    set_story_worker,
)


@pytest.fixture
async def live_worker(real_redis):
    """Give every bound worker in these tests the Redis record a live one has.

    A binding is reusable only while its worker still has evidence of existing:
    a worker whose `worker:status` and `worker:meta` are both gone is a deleted
    one, and the registry evicts that binding instead of handing it back. These
    tests are about the mapping, so they register their workers for real.
    """
    registered: list[str] = []

    async def register(worker_id: str) -> str:
        await real_redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})
        registered.append(worker_id)
        return worker_id

    yield register
    for worker_id in registered:
        await real_redis.delete(f"worker:status:{worker_id}")


class TestStoryWorkerRegistryReal:
    """Registry CRUD against real Redis."""

    @pytest.mark.asyncio
    async def test_set_and_get_worker(self, real_redis, live_worker):
        """Store worker_id, retrieve it back."""
        await set_story_worker(real_redis, "story-svc-1", await live_worker("dev-worker-abc"))

        result = await get_story_worker(real_redis, "story-svc-1")
        assert result == "dev-worker-abc"

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self, real_redis):
        """Non-existent story returns None."""
        result = await get_story_worker(real_redis, "story-nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_clear_removes_worker(self, real_redis, live_worker):
        """Clear removes the mapping, get returns None."""
        await set_story_worker(real_redis, "story-svc-2", await live_worker("dev-worker-xyz"))
        await clear_story_worker(real_redis, "story-svc-2")

        result = await get_story_worker(real_redis, "story-svc-2")
        assert result is None

    @pytest.mark.asyncio
    async def test_overwrite_worker(self, real_redis, live_worker):
        """Setting worker twice overwrites the old value."""
        await set_story_worker(real_redis, "story-svc-3", await live_worker("dev-old"))
        await set_story_worker(real_redis, "story-svc-3", await live_worker("dev-new"))

        result = await get_story_worker(real_redis, "story-svc-3")
        assert result == "dev-new"

    @pytest.mark.asyncio
    async def test_multiple_stories_independent(self, real_redis, live_worker):
        """Different stories have independent worker mappings."""
        await set_story_worker(real_redis, "story-a", await live_worker("dev-1"))
        await set_story_worker(real_redis, "story-b", await live_worker("dev-2"))

        assert await get_story_worker(real_redis, "story-a") == "dev-1"
        assert await get_story_worker(real_redis, "story-b") == "dev-2"

        await clear_story_worker(real_redis, "story-a")
        assert await get_story_worker(real_redis, "story-a") is None
        assert await get_story_worker(real_redis, "story-b") == "dev-2"

    @pytest.mark.asyncio
    async def test_cleanup_sends_delete_command_to_stream(self, real_redis, live_worker):
        """Simulates _cleanup_story_worker flow: xadd to worker:commands stream."""
        # Set up a worker in registry
        await set_story_worker(real_redis, "story-cleanup", await live_worker("dev-cleanup-abc"))

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
