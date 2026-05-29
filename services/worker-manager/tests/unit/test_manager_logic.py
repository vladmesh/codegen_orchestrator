import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import uuid
from fakeredis import aioredis

from shared.contracts.dto.worker import WorkerStatus
from shared.redis import decode_redis_fields
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
async def test_network_selection_uses_worker_network():
    """When DOCKER_NETWORK is empty, workers should connect to WORKER_NETWORK, not INTERNAL_NETWORK."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    wrapper.exec_in_container = AsyncMock(return_value=(0, "ok"))

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"),
        patch("src.manager.workspace_mod.get_scaffolded_workspace", return_value=(Path("/data/ws/repo-1"), True)),
    ):
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.INTERNAL_NETWORK = "codegen_internal"
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_REDIS_URL = ""
        mock_settings.WORKER_API_URL = ""
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"

        await manager.create_worker_with_capabilities(
            worker_id="w1",
            capabilities=["git"],
            base_image="worker-base:latest",
            agent_type="claude",
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

    # run_container should have been called with network="codegen_worker"
    run_call = wrapper.run_container.call_args
    assert run_call.kwargs.get("network") == "codegen_worker" or run_call[1].get("network") == "codegen_worker"


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

    meta = decode_redis_fields(await redis.hgetall(f"worker:meta:{worker_id}"))
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
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.RUNNING})
    await redis.set(f"worker:error:{worker_id}", "some error")
    await redis.set(f"worker:last_activity:{worker_id}", "12345")

    with patch("src.manager.ComposeRunner") as mock_runner_cls:
        # Mock compose runner to avoid filesystem side effects
        mock_runner = MagicMock()
        mock_runner.run = AsyncMock(return_value=(0, "", ""))
        mock_runner_cls.return_value = mock_runner

        await manager.delete_worker(worker_id)

    # Network should be removed
    wrapper.remove_network.assert_awaited_with(f"dev_proj_{worker_id}")

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
async def test_gc_does_not_remove_workspaces():
    """Orphan GC should not remove workspaces (scaffolded workspaces are managed by time-based GC)."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    wrapper.list_containers = AsyncMock(return_value=[])
    wrapper.list_networks = AsyncMock(return_value=[])

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with patch("src.manager.workspace_mod.remove_workspace") as mock_rm_ws:
        await manager.garbage_collect_orphaned_resources()

    mock_rm_ws.assert_not_called()


@pytest.mark.asyncio
async def test_gc_skips_known_workers():
    """GC should not remove resources belonging to known workers."""
    redis = aioredis.FakeRedis(decode_responses=True)
    await redis.hset("worker:status:alive-1", mapping={"status": WorkerStatus.RUNNING})

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


# --- Stale Worker Cleanup Tests ---


@pytest.mark.asyncio
async def test_check_project_lock_cleans_dead_worker():
    """_check_project_lock should auto-cleanup DEAD workers and return None (unlocked)."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    project_id = "proj-stale-dead"
    worker_id = "worker-dead-123"

    # Simulate stale state: project in active set, worker keys exist but status is DEAD
    await redis.sadd("workspace:active_projects", project_id)
    await redis.hset(f"worker:meta:{worker_id}", mapping={"project_id": project_id})
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.DEAD})

    result = await manager._check_project_lock(project_id)

    # Should return None (project is free) after cleaning stale keys
    assert result is None
    # Stale keys should be cleaned up
    assert await redis.hgetall(f"worker:meta:{worker_id}") == {}
    assert await redis.hgetall(f"worker:status:{worker_id}") == {}
    assert not await redis.sismember("workspace:active_projects", project_id)


@pytest.mark.asyncio
async def test_check_project_lock_cleans_failed_worker():
    """_check_project_lock should auto-cleanup FAILED workers."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    project_id = "proj-stale-failed"
    worker_id = "worker-failed-456"

    await redis.sadd("workspace:active_projects", project_id)
    await redis.hset(f"worker:meta:{worker_id}", mapping={"project_id": project_id})
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.FAILED})

    result = await manager._check_project_lock(project_id)

    assert result is None
    assert await redis.hgetall(f"worker:meta:{worker_id}") == {}


@pytest.mark.asyncio
async def test_check_project_lock_cleans_stopped_worker():
    """_check_project_lock should auto-cleanup STOPPED workers."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    project_id = "proj-stale-stopped"
    worker_id = "worker-stopped-789"

    await redis.sadd("workspace:active_projects", project_id)
    await redis.hset(f"worker:meta:{worker_id}", mapping={"project_id": project_id})
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.STOPPED})

    result = await manager._check_project_lock(project_id)

    assert result is None


@pytest.mark.asyncio
async def test_check_project_lock_keeps_running_worker():
    """_check_project_lock should NOT clean up RUNNING workers."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    project_id = "proj-active"
    worker_id = "worker-running-abc"

    await redis.sadd("workspace:active_projects", project_id)
    await redis.hset(f"worker:meta:{worker_id}", mapping={"project_id": project_id})
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": WorkerStatus.RUNNING})

    result = await manager._check_project_lock(project_id)

    # Should return the worker_id — project is locked
    assert result == worker_id
    # Keys should remain
    assert await redis.hgetall(f"worker:meta:{worker_id}") != {}


@pytest.mark.asyncio
async def test_check_project_lock_keeps_starting_worker():
    """_check_project_lock should NOT clean up STARTING workers."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    project_id = "proj-starting"
    worker_id = "worker-starting-def"

    await redis.sadd("workspace:active_projects", project_id)
    await redis.hset(f"worker:meta:{worker_id}", mapping={"project_id": project_id})
    await redis.hset(f"worker:status:{worker_id}", mapping={"status": "STARTING"})

    result = await manager._check_project_lock(project_id)

    assert result == worker_id


# --- Branch Checkout Tests ---


@pytest.mark.asyncio
async def test_checkout_branch_called_when_branch_provided():
    """create_worker_with_capabilities with branch should call _checkout_branch."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    wrapper.exec_in_container = AsyncMock(return_value=(0, "ok"))

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"),
        patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/data/ws/repo-1"), True),
        ),
    ):
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_REDIS_URL = ""
        mock_settings.WORKER_API_URL = ""
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"

        await manager.create_worker_with_capabilities(
            worker_id="w-branch-test",
            capabilities=["git"],
            base_image="worker-base:latest",
            agent_type="claude",
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            branch="story/story-abc",
        )

    # Verify _checkout_branch was called — the actual git command is base64-encoded,
    # so we decode one of the exec calls to check the branch name is present
    import base64 as b64

    exec_calls = wrapper.exec_in_container.call_args_list
    decoded_cmds = []
    for c in exec_calls:
        cmd_str = c.args[1] if len(c.args) > 1 else ""
        # Extract base64 payload from "bash -c 'echo <b64> | base64 -d | bash'"
        if "base64 -d" in cmd_str:
            parts = cmd_str.split("echo ", 1)
            if len(parts) > 1:
                b64_part = parts[1].split(" |")[0].strip()
                try:
                    decoded_cmds.append(b64.b64decode(b64_part).decode())
                except Exception:
                    pass
    branch_cmds = [d for d in decoded_cmds if "story/story-abc" in d]
    assert len(branch_cmds) > 0, f"No branch checkout found. Decoded cmds: {decoded_cmds}"


@pytest.mark.asyncio
async def test_no_checkout_branch_when_branch_is_none():
    """create_worker_with_capabilities without branch should NOT call _checkout_branch."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    wrapper.exec_in_container = AsyncMock(return_value=(0, "ok"))

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"),
        patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/data/ws/repo-1"), True),
        ),
    ):
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_REDIS_URL = ""
        mock_settings.WORKER_API_URL = ""
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"

        await manager.create_worker_with_capabilities(
            worker_id="w-no-branch",
            capabilities=["git"],
            base_image="worker-base:latest",
            agent_type="claude",
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

    # No exec call should contain "story/" or "checkout -b"
    exec_calls = wrapper.exec_in_container.call_args_list
    branch_calls = [c for c in exec_calls if "checkout -b" in str(c)]
    assert len(branch_calls) == 0, f"Unexpected branch checkout call found: {branch_calls}"
