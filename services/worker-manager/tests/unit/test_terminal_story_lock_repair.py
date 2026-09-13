from pathlib import Path
from unittest.mock import AsyncMock, patch

from fakeredis import aioredis
import pytest

from shared.contracts.queues.worker import WorkerOwnership
from src.manager import WorkerManager

PROJECT = "project-1"


def ownership(story: str) -> WorkerOwnership:
    return WorkerOwnership(
        story_id=story, project_id=PROJECT, run_id=f"run-{story}", attempt_id=f"attempt-{story}"
    )


@pytest.fixture
def docker():
    client = AsyncMock()
    client.image_exists.return_value = True
    return client


@pytest.fixture
async def manager(docker):
    redis = aioredis.FakeRedis(decode_responses=True)
    value = WorkerManager(redis, docker_client=docker)
    await redis.set(f"workspace:lock:{PROJECT}", "old-worker")
    await redis.hset(
        "worker:meta:old-worker",
        mapping={**ownership("old-story").as_redis_meta(), "worker_type": "developer"},
    )
    return value


@pytest.mark.asyncio
async def test_terminal_owner_is_torn_down_and_pending_create_continues(manager):
    manager._lookup_story_status = AsyncMock(return_value="completed")
    manager.delete_worker = AsyncMock()

    async def release(*_, **__):
        await manager.redis.delete(f"workspace:lock:{PROJECT}")

    manager.delete_worker.side_effect = release
    with patch(
        "src.manager.workspace_mod.get_scaffolded_workspace", return_value=(Path("/ws"), True)
    ):
        assert await manager._find_developer_workspace(PROJECT, "repo-1") == Path("/ws")

    manager.delete_worker.assert_awaited_once_with("old-worker", reason="completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["in_progress", "testing"])
async def test_live_owner_refusal_names_worker_and_story(manager, status):
    manager._lookup_story_status = AsyncMock(return_value=status)
    manager.delete_worker = AsyncMock()

    with pytest.raises(RuntimeError, match="old-worker.*old-story"):
        await manager._find_developer_workspace(PROJECT, "repo-1")

    manager.delete_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownerless_legacy_lock_fails_closed(manager):
    await manager.redis.hdel("worker:meta:old-worker", "story_id")
    manager._lookup_story_status = AsyncMock()
    manager.delete_worker = AsyncMock()

    with pytest.raises(RuntimeError, match="old-worker.*unknown owning story"):
        await manager._find_developer_workspace(PROJECT, "repo-1")

    manager._lookup_story_status.assert_not_awaited()
    manager.delete_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_lookup_failure_fails_closed(manager):
    manager._lookup_story_status = AsyncMock(side_effect=RuntimeError("API unavailable"))
    manager.delete_worker = AsyncMock()

    with pytest.raises(RuntimeError, match="old-worker.*old-story.*status lookup failed"):
        await manager._find_developer_workspace(PROJECT, "repo-1")

    manager.delete_worker.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_lookup_uses_authenticated_internal_api(manager):
    response = AsyncMock()
    response.json = lambda: {"status": "completed"}
    client = AsyncMock()
    client.request.return_value = response
    with patch("src.manager.InternalAPIClient", return_value=client) as client_type:
        assert await manager._lookup_story_status("old-story") == "completed"

    client_type.assert_called_once()
    client.request.assert_awaited_once_with("GET", "stories/old-story")
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_replacement_lock_after_terminal_delete_is_not_released_or_accepted(manager):
    manager._lookup_story_status = AsyncMock(return_value="archived")
    manager.delete_worker = AsyncMock()

    async def replace(*_, **__):
        await manager.redis.set(f"workspace:lock:{PROJECT}", "replacement-worker")

    manager.delete_worker.side_effect = replace
    with pytest.raises(RuntimeError, match="replacement-worker"):
        await manager._find_developer_workspace(PROJECT, "repo-1")

    assert await manager.redis.get(f"workspace:lock:{PROJECT}") == "replacement-worker"


@pytest.mark.asyncio
async def test_pre_container_refusal_remains_observable_but_is_bounded(manager):
    await manager._reject_worker("refused-worker", RuntimeError("project conflict"))

    assert await manager.redis.hget("worker:status:refused-worker", "status") == "FAILED"
    assert await manager.redis.get("worker:error:refused-worker") == "project conflict"
    assert 0 < await manager.redis.ttl("worker:status:refused-worker") <= 300
    assert 0 < await manager.redis.ttl("worker:error:refused-worker") <= 300
