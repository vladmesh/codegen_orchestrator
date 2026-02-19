import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import uuid
from fakeredis import aioredis

from src.manager import WorkerManager


def _make_docker_mock():
    wrapper = MagicMock()
    wrapper.image_exists = AsyncMock(return_value=True)
    wrapper.remove_container = AsyncMock()
    container = MagicMock()
    container.id = "test-id"
    wrapper.run_container = AsyncMock(return_value=container)
    wrapper.create_network = AsyncMock()
    wrapper.connect_network = AsyncMock()
    wrapper.remove_network = AsyncMock()
    wrapper.pause_container = AsyncMock()
    wrapper.unpause_container = AsyncMock()
    return wrapper


@pytest.mark.asyncio
async def test_create_worker_unit():
    redis = MagicMock()
    redis.set = AsyncMock()
    redis.hset = AsyncMock()

    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    worker_id = str(uuid.uuid4())
    res = await manager.create_worker(worker_id, "worker:latest")

    assert res == "test-id"
    wrapper.run_container.assert_awaited_once()
    redis.set.assert_awaited()


@pytest.mark.asyncio
async def test_create_worker_creates_dev_network():
    """create_worker with create_dev_network=True should create a dev_proj_<id> network."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)
    worker_id = "worker-net-test"

    await manager.create_worker(worker_id, "worker:latest", network_name="codegen_internal", create_dev_network=True)

    wrapper.create_network.assert_awaited_once_with(f"dev_proj_{worker_id}")


@pytest.mark.asyncio
async def test_create_worker_connects_to_both_networks():
    """Container should be connected to the dev network after creation."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)
    worker_id = "worker-dual-net"

    await manager.create_worker(worker_id, "worker:latest", network_name="codegen_internal", create_dev_network=True)

    # Should have been called to attach to the dev network
    wrapper.connect_network.assert_awaited_once_with(f"dev_proj_{worker_id}", "test-id")


@pytest.mark.asyncio
async def test_create_worker_creates_workspace_dir():
    """create_worker should store workspace_path in Redis metadata."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)
    worker_id = "worker-ws-test"

    await manager.create_worker(
        worker_id,
        "worker:latest",
        network_name="codegen_internal",
        create_dev_network=True,
        workspace_path="/tmp/codegen/workspaces/worker-ws-test/workspace",
    )

    meta = await redis.hgetall(f"worker:meta:{worker_id}")
    assert meta["workspace_path"] == "/tmp/codegen/workspaces/worker-ws-test/workspace"
    assert meta["dev_network"] == f"dev_proj_{worker_id}"


@pytest.mark.asyncio
async def test_delete_worker_full_cleanup():
    """delete_worker should remove network, workspace, and Redis keys."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)
    worker_id = "worker-del-test"

    # Pre-populate Redis with metadata
    await redis.hset(
        f"worker:meta:{worker_id}",
        mapping={
            "dev_network": f"dev_proj_{worker_id}",
            "workspace_path": f"/tmp/codegen/workspaces/{worker_id}/workspace",
        },
    )
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": "RUNNING"})
    await redis.set(f"worker:error:{worker_id}", "some error")
    await redis.set(f"worker:last_activity:{worker_id}", "12345")

    with (
        patch("src.manager.workspace_mod.remove_workspace") as mock_rm_ws,
        patch("src.manager.ComposeRunner") as mock_runner_cls,
    ):
        # Mock compose runner to avoid filesystem side effects
        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(return_value=(0, "", ""))
        mock_runner_cls.return_value = mock_runner

        await manager.delete_worker(worker_id)

    # Network should be removed
    wrapper.remove_network.assert_awaited_with(f"dev_proj_{worker_id}")

    # Workspace should be removed
    mock_rm_ws.assert_called_once()

    # Redis keys should be deleted
    assert await redis.hgetall(f"worker:meta:{worker_id}") == {}
    assert await redis.hgetall(f"worker:status:{worker_id}") == {}
    assert await redis.get(f"worker:error:{worker_id}") is None
    assert await redis.get(f"worker:last_activity:{worker_id}") is None
