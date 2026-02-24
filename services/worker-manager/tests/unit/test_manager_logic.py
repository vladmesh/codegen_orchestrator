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


# --- Orphaned Resource GC Tests ---


@pytest.mark.asyncio
async def test_gc_removes_orphaned_container():
    """GC should call delete_worker for containers not in Redis."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    # Mock an orphaned container with worker labels
    orphan_container = MagicMock()
    orphan_container.labels = {
        "com.codegen.type": "worker",
        "com.codegen.worker.id": "orphan-1",
    }
    wrapper.list_containers = AsyncMock(return_value=[orphan_container])
    wrapper.list_networks = AsyncMock(return_value=[])

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.workspace_mod.remove_workspace"),
        patch("src.manager.ComposeRunner") as mock_runner_cls,
        patch("os.listdir", return_value=[]),
    ):
        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(return_value=(0, "", ""))
        mock_runner_cls.return_value = mock_runner

        await manager.garbage_collect_orphaned_resources()

    # delete_worker removes the container
    wrapper.remove_container.assert_awaited()


@pytest.mark.asyncio
async def test_gc_removes_orphaned_network():
    """GC should remove dev_proj_ networks not in Redis."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    wrapper.list_containers = AsyncMock(return_value=[])

    # Mock an orphaned network
    orphan_net = MagicMock()
    orphan_net.name = "dev_proj_orphan-2"
    wrapper.list_networks = AsyncMock(return_value=[orphan_net])

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with patch("os.listdir", return_value=[]):
        await manager.garbage_collect_orphaned_resources()

    wrapper.remove_network.assert_awaited_with("dev_proj_orphan-2")


@pytest.mark.asyncio
async def test_gc_removes_orphaned_workspace():
    """GC should remove workspace directories not in Redis."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    wrapper.list_containers = AsyncMock(return_value=[])
    wrapper.list_networks = AsyncMock(return_value=[])

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("os.listdir", return_value=["orphan-3"]),
        patch("src.manager.workspace_mod.remove_workspace") as mock_rm_ws,
    ):
        await manager.garbage_collect_orphaned_resources()

    from src.config import settings

    mock_rm_ws.assert_called_once_with(settings.WORKSPACE_BASE_PATH, "orphan-3")


@pytest.mark.asyncio
async def test_gc_skips_known_workers():
    """GC should not remove resources belonging to known workers."""
    redis = aioredis.FakeRedis(decode_responses=True)
    await redis.hset("worker:status:alive-1", mapping={"status": "RUNNING"})

    wrapper = _make_docker_mock()

    # Container for alive-1
    alive_container = MagicMock()
    alive_container.labels = {
        "com.codegen.type": "worker",
        "com.codegen.worker.id": "alive-1",
    }
    wrapper.list_containers = AsyncMock(return_value=[alive_container])

    # Network for alive-1
    alive_net = MagicMock()
    alive_net.name = "dev_proj_alive-1"
    wrapper.list_networks = AsyncMock(return_value=[alive_net])

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("os.listdir", return_value=["alive-1"]),
        patch("src.manager.workspace_mod.remove_workspace") as mock_rm_ws,
    ):
        await manager.garbage_collect_orphaned_resources()

    # Nothing should be deleted — container removal not called, network not removed, workspace not removed
    wrapper.remove_container.assert_not_awaited()
    wrapper.remove_network.assert_not_awaited()
    mock_rm_ws.assert_not_called()
