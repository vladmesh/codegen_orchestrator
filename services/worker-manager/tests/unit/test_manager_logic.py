import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import uuid
from fakeredis import aioredis

from shared.contracts.dto.worker import WorkerStatus
from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from shared.redis import decode_redis_fields
from src.manager import WorkerManager


# Every worker is created for somebody. These tests are not about who, so they
# use one owner; the tests that are about ownership name their own.
_OWNERSHIP = WorkerOwnership(project_id="proj-test", run_id="eng-test", attempt_id="attempt-eng-test")


def _make_docker_mock():
    wrapper = MagicMock()
    wrapper.image_exists = AsyncMock(return_value=True)
    wrapper.get_image_label = AsyncMock(return_value="basehash0001")
    wrapper.remove_container = AsyncMock()
    container = MagicMock()
    container.id = "test-id"
    wrapper.run_container = AsyncMock(return_value=container)
    wrapper.create_network = AsyncMock()
    wrapper.connect_network = AsyncMock()
    wrapper.remove_network = AsyncMock()
    wrapper.pause_container = AsyncMock()
    wrapper.unpause_container = AsyncMock()
    wrapper.get_container_logs = AsyncMock(return_value="")
    return wrapper


@pytest.mark.asyncio
async def test_remote_docker_prepares_mounts_in_the_daemon_namespace(monkeypatch):
    """A remote daemon must chown the paths it, rather than the manager container, mounts."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker:2375")

    await manager._prepare_remote_daemon_mounts(
        image="worker:latest",
        worker_id="worker-1",
        workspace_path="/data/workspaces/repo-1",
        transcript_path="/data/worker-transcripts",
    )

    wrapper.run_container.assert_awaited_once_with(
        "worker:latest",
        name="worker-mount-prep-worker-1",
        entrypoint="/bin/chown",
        command=["-R", "1000:1000", "/workspace", "/artifacts/worker-transcripts"],
        user="root",
        network_mode="none",
        volumes={
            "/data/workspaces/repo-1": {"bind": "/workspace", "mode": "rw"},
            "/data/worker-transcripts": {
                "bind": "/artifacts/worker-transcripts",
                "mode": "rw",
            },
        },
        remove=True,
        read_only=True,
        cap_drop=["ALL"],
        cap_add=["CHOWN", "DAC_OVERRIDE"],
        security_opt=["no-new-privileges:true"],
        pids_limit=32,
        mem_limit="64m",
    )


@pytest.mark.asyncio
async def test_instruction_injection_failure_aborts_worker_creation():
    """A worker without its instruction file is failed now, not ACKed into a timeout."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    wrapper.exec_in_container = AsyncMock(return_value=(1, b"permission denied"))
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(
            manager,
            "ensure_or_build_image",
            new_callable=AsyncMock,
            return_value="worker:latest",
        ),
        patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/data/ws/repo-1"), True),
        ),
    ):
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_BROKER_URL = "http://worker-broker:8001"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"
        mock_settings.WORKER_TRANSCRIPT_STORAGE_PATH = "/data/worker-transcripts"
        mock_settings.WORKER_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024
        mock_settings.WORKER_TRANSCRIPT_RETENTION_DAYS = 30

        with pytest.raises(RuntimeError, match="could not inject /workspace/CLAUDE.md"):
            await manager.create_worker_with_capabilities(
                worker_id="w-injection-failure",
                capabilities=["git"],
                base_image="worker-base:latest",
                ownership=_OWNERSHIP,
                agent_type=AgentType.CLAUDE,
                auth_mode="api_key",
                api_key="test-api-key",
                instructions="required instructions",
                repo_id="repo-1",
            )


@pytest.mark.asyncio
async def test_create_worker_unit():
    redis = MagicMock()
    redis.set = AsyncMock()
    redis.hset = AsyncMock()

    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)

    worker_id = str(uuid.uuid4())
    res = await manager.create_worker(worker_id, "worker:latest", ownership=_OWNERSHIP)

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
        mock_settings.WORKER_BROKER_URL = "http://worker-broker:8001"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"

        await manager.create_worker_with_capabilities(
            worker_id="w1",
            capabilities=["git"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            agent_type="claude",
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

    # run_container should have been called with network="codegen_worker"
    run_call = wrapper.run_container.call_args
    assert run_call.kwargs.get("network") == "codegen_worker" or run_call[1].get("network") == "codegen_worker"
    container_env = run_call.kwargs.get("environment") or run_call[1]["environment"]
    assert container_env["WORKER_BROKER_URL"] == "http://worker-broker:8001"
    assert "WORKER_REDIS_URL" not in container_env
    assert "WORKER_API_URL" not in container_env


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [AgentType.CLAUDE, AgentType.CODEX, AgentType.FACTORY])
async def test_production_launch_uses_hardened_container_config(agent_type):
    """The real production launch passes the config's hardening to Docker."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    wrapper.exec_in_container = AsyncMock(return_value=(0, "ok"))
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"),
        patch("src.manager.workspace_mod.get_scaffolded_workspace", return_value=(Path("/data/ws/repo-1"), True)),
    ):
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_REDIS_URL = "redis://worker-redis:6379/0"
        mock_settings.WORKER_API_URL = "http://worker-api:8000"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"
        mock_settings.WORKER_TRANSCRIPT_STORAGE_PATH = "/data/worker-transcripts"
        mock_settings.WORKER_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024

        await manager.create_worker_with_capabilities(
            worker_id=f"w-{agent_type.value}",
            capabilities=["git"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            agent_type=agent_type,
            auth_mode="api_key",
            api_key="test-api-key",
            repo_id="repo-1",
        )

    kwargs = wrapper.run_container.call_args.kwargs
    assert kwargs["network"] == "codegen_worker"
    assert "network_mode" not in kwargs
    assert kwargs["mem_limit"] == "4g"
    assert kwargs["cpu_period"] == 100000
    assert kwargs["cpu_quota"] == 100000
    assert kwargs["pids_limit"] > 0
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]


@pytest.mark.asyncio
async def test_production_launch_rejects_host_network_configuration():
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"),
        patch("src.manager.workspace_mod.get_scaffolded_workspace", return_value=(Path("/data/ws/repo-1"), True)),
    ):
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DOCKER_NETWORK = "host"
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_REDIS_URL = "redis://worker-redis:6379/0"
        mock_settings.WORKER_API_URL = "http://worker-api:8000"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"
        mock_settings.WORKER_TRANSCRIPT_STORAGE_PATH = "/data/worker-transcripts"
        mock_settings.WORKER_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024

        with pytest.raises(RuntimeError, match="DOCKER_NETWORK=host"):
            await manager.create_worker_with_capabilities(
                worker_id="w-host-network",
                capabilities=["git"],
                base_image="worker-base:latest",
                ownership=_OWNERSHIP,
                agent_type=AgentType.CLAUDE,
                auth_mode="api_key",
                api_key="test-api-key",
                repo_id="repo-1",
            )

    wrapper.run_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_dind_launch_keeps_explicit_test_host_network_compatibility():
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    wrapper.exec_in_container = AsyncMock(return_value=(0, "ok"))
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"),
        patch("src.manager.workspace_mod.get_scaffolded_workspace", return_value=(Path("/data/ws/repo-1"), True)),
    ):
        mock_settings.ENVIRONMENT = "test"
        mock_settings.DOCKER_NETWORK = "host"
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_REDIS_URL = "redis://worker-redis:6379/0"
        mock_settings.WORKER_API_URL = "http://worker-api:8000"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"
        mock_settings.WORKER_TRANSCRIPT_STORAGE_PATH = "/data/worker-transcripts"
        mock_settings.WORKER_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024

        await manager.create_worker_with_capabilities(
            worker_id="w-dind-host-network",
            capabilities=["git"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            agent_type=AgentType.CLAUDE,
            auth_mode="api_key",
            api_key="test-api-key",
            repo_id="repo-1",
        )

    kwargs = wrapper.run_container.call_args.kwargs
    assert kwargs["network_mode"] == "host"
    assert "network" not in kwargs
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]


@pytest.mark.asyncio
async def test_ownership_preparation_failure_aborts_before_container_launch():
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)

    with (
        patch("src.manager.settings") as mock_settings,
        patch.object(manager, "ensure_or_build_image", new_callable=AsyncMock, return_value="worker:latest"),
        patch("src.manager.workspace_mod.get_scaffolded_workspace", return_value=(Path("/data/ws/repo-1"), True)),
        patch("src.manager.workspace_mod.prepare_worker_paths", side_effect=RuntimeError("ownership failed")),
    ):
        mock_settings.ENVIRONMENT = "production"
        mock_settings.DOCKER_NETWORK = ""
        mock_settings.WORKER_NETWORK = "codegen_worker"
        mock_settings.SCAFFOLDED_WORKSPACE_PATH = "/data/ws"
        mock_settings.WORKER_REDIS_URL = "redis://worker-redis:6379/0"
        mock_settings.WORKER_API_URL = "http://worker-api:8000"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"
        mock_settings.WORKER_TRANSCRIPT_STORAGE_PATH = "/data/worker-transcripts"
        mock_settings.WORKER_TRANSCRIPT_MAX_BYTES = 5 * 1024 * 1024

        with pytest.raises(RuntimeError, match="ownership failed"):
            await manager.create_worker_with_capabilities(
                worker_id="w-ownership-failure",
                capabilities=["git"],
                base_image="worker-base:latest",
                ownership=_OWNERSHIP,
                agent_type=AgentType.CLAUDE,
                auth_mode="api_key",
                api_key="test-api-key",
                repo_id="repo-1",
            )

    wrapper.run_container.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_worker_creates_dev_network():
    """create_worker with create_dev_network=True should create a dev_proj_<id> network."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)
    worker_id = "worker-net-test"

    await manager.create_worker(
        worker_id, "worker:latest", ownership=_OWNERSHIP, network_name="codegen_internal", create_dev_network=True
    )

    wrapper.create_network.assert_awaited_once_with(f"dev_proj_{worker_id}")


@pytest.mark.asyncio
async def test_create_worker_connects_to_both_networks():
    """Container should be connected to the dev network after creation."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()

    manager = WorkerManager(redis=redis, docker_client=wrapper)
    worker_id = "worker-dual-net"

    await manager.create_worker(
        worker_id, "worker:latest", ownership=_OWNERSHIP, network_name="codegen_internal", create_dev_network=True
    )

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
        ownership=_OWNERSHIP,
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


@pytest.mark.asyncio
async def test_delete_worker_uses_real_runner_recovery_profile(tmp_path):
    """Teardown remains project-scoped even when the workspace manifest is hostile."""
    redis = aioredis.FakeRedis(decode_responses=True)
    wrapper = _make_docker_mock()
    manager = WorkerManager(redis=redis, docker_client=wrapper)
    worker_id = "worker-real-recovery"
    workspace = tmp_path / "project" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "compose.yml").write_text("services: [malformed]\n")
    await redis.hset(
        f"worker:meta:{worker_id}",
        mapping={"dev_network": f"dev_proj_{worker_id}", "workspace_path": str(workspace)},
    )

    result = MagicMock(returncode=0, stdout="", stderr="")
    with (
        patch("src.manager.settings.SCAFFOLDED_WORKSPACE_PATH", str(tmp_path)),
        patch("src.compose_runner.subprocess.run", return_value=result) as mock_run,
    ):
        await manager.delete_worker(worker_id)

    command = mock_run.call_args.args[0]
    assert Path(command[0]).is_absolute()
    assert command[:4] == [command[0], "compose", "--project-name", f"worker_{worker_id}"]
    assert command[4:6] == ["--project-directory", str(tmp_path / ".compose-plans" / worker_id)]
    assert command[6:] == ["down", "-v"]
    assert "-f" not in command
    assert mock_run.call_args.kwargs["cwd"] == str(tmp_path / ".compose-plans" / worker_id)
    assert not (tmp_path / ".compose-plans" / worker_id).exists()
    wrapper.remove_container.assert_awaited_with(f"worker-{worker_id}", force=True)
    wrapper.remove_network.assert_awaited_with(f"dev_proj_{worker_id}")


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
        mock_settings.WORKER_REDIS_URL = "redis://worker-redis:6379/0"
        mock_settings.WORKER_API_URL = "http://worker-api:8000"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"

        await manager.create_worker_with_capabilities(
            worker_id="w-branch-test",
            capabilities=["git"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
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
        mock_settings.WORKER_REDIS_URL = "redis://worker-redis:6379/0"
        mock_settings.WORKER_API_URL = "http://worker-api:8000"
        mock_settings.WORKER_SUBPROCESS_TIMEOUT_SECONDS = 300
        mock_settings.WORKER_MANAGER_URL = "http://worker-manager:8000"
        mock_settings.WORKER_IMAGE_PREFIX = "worker"
        mock_settings.WORKER_DOCKER_LABELS = "{}"

        await manager.create_worker_with_capabilities(
            worker_id="w-no-branch",
            capabilities=["git"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            agent_type="claude",
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

    # No exec call should contain "story/" or "checkout -b"
    exec_calls = wrapper.exec_in_container.call_args_list
    branch_calls = [c for c in exec_calls if "checkout -b" in str(c)]
    assert len(branch_calls) == 0, f"Unexpected branch checkout call found: {branch_calls}"
