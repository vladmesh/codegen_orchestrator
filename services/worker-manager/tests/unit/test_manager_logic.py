import pytest
from unittest.mock import MagicMock, AsyncMock
import uuid
from src.manager import WorkerManager


@pytest.mark.asyncio
async def test_create_worker_unit():
    redis = MagicMock()
    redis.set = AsyncMock()

    wrapper = MagicMock()
    wrapper.run_container = AsyncMock()
    container = MagicMock()
    container.id = "test-id"
    wrapper.run_container.return_value = container

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    worker_id = str(uuid.uuid4())
    res = await manager.create_worker(worker_id, "worker:latest")

    assert res == "test-id"
    wrapper.run_container.assert_awaited_once()
    redis.set.assert_awaited()
