"""Tests for project_id passthrough from consumer to manager."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from fakeredis import aioredis

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    DeleteWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)

from src.config import settings
from src.consumer import WorkerCommandConsumer
from src.manager import WorkerManager


def _make_create_command(project_id: str | None = None) -> CreateWorkerCommand:
    """Build a CreateWorkerCommand with optional project_id."""
    config = WorkerConfig(
        name="test-worker",
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="test instructions",
        allowed_commands=["*"],
        capabilities=[WorkerCapability.GIT],
        project_id=project_id,
    )
    return CreateWorkerCommand(
        request_id="req-001",
        config=config,
        context={"source": "test"},
    )


@pytest.fixture
def consumer():
    """Consumer with mocked redis stream client and manager."""
    client = MagicMock()
    client.redis = MagicMock()
    client.redis.xadd = AsyncMock()
    manager = MagicMock()
    manager.create_worker_with_capabilities = AsyncMock(return_value="test-worker")
    return WorkerCommandConsumer(client=client, manager=manager)


@pytest.mark.asyncio
async def test_consumer_passes_reason_to_manager():
    """reason from DeleteWorkerCommand should be forwarded to manager.delete_worker."""
    client = MagicMock()
    client.redis = MagicMock()
    client.redis.xadd = AsyncMock()
    manager = MagicMock()
    manager.delete_worker = AsyncMock()

    consumer = WorkerCommandConsumer(client=client, manager=manager)
    cmd = DeleteWorkerCommand(request_id="req-del", worker_id="w-1", reason="failed")
    await consumer._handle_delete(cmd)

    manager.delete_worker.assert_awaited_once_with("w-1", reason="failed")


@pytest.mark.asyncio
async def test_consumer_passes_none_reason_when_missing():
    """When DeleteWorkerCommand has no reason, None should be forwarded."""
    client = MagicMock()
    client.redis = MagicMock()
    client.redis.xadd = AsyncMock()
    manager = MagicMock()
    manager.delete_worker = AsyncMock()

    consumer = WorkerCommandConsumer(client=client, manager=manager)
    cmd = DeleteWorkerCommand(request_id="req-del", worker_id="w-1")
    await consumer._handle_delete(cmd)

    manager.delete_worker.assert_awaited_once_with("w-1", reason=None)


@pytest.mark.asyncio
async def test_consumer_passes_project_id_to_manager(consumer):
    """project_id from WorkerConfig should be forwarded to manager."""
    cmd = _make_create_command(project_id="proj-123")
    await consumer._handle_create(cmd)

    consumer.manager.create_worker_with_capabilities.assert_awaited_once()
    call_kwargs = consumer.manager.create_worker_with_capabilities.call_args.kwargs
    assert call_kwargs["project_id"] == "proj-123"


@pytest.mark.asyncio
async def test_consumer_passes_none_project_id_when_missing(consumer):
    """When WorkerConfig has no project_id, None should be forwarded."""
    cmd = _make_create_command()  # project_id defaults to None
    await consumer._handle_create(cmd)

    consumer.manager.create_worker_with_capabilities.assert_awaited_once()
    call_kwargs = consumer.manager.create_worker_with_capabilities.call_args.kwargs
    assert call_kwargs["project_id"] is None


# --- Phase 2: Workspace by project_id ---


def _make_docker_mock():
    """Create a fully-mocked DockerClientWrapper."""
    docker = MagicMock()
    docker.image_exists = AsyncMock(return_value=True)
    docker.build_image = AsyncMock()
    docker.remove_container = AsyncMock()
    docker.create_network = AsyncMock()
    docker.connect_network = AsyncMock()
    docker.remove_network = AsyncMock()
    docker.exec_in_container = AsyncMock(return_value=(0, ""))
    docker.get_container_logs = AsyncMock(return_value="")
    container = MagicMock()
    container.id = "container-abc"
    docker.run_container = AsyncMock(return_value=container)
    return docker


class TestWorkspaceByProjectId:
    """Tests for workspace routing based on project_id."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.hset = AsyncMock()
        redis.hget = AsyncMock(return_value=None)
        redis.sadd = AsyncMock()
        redis.sismember = AsyncMock(return_value=False)
        return redis

    @pytest.mark.asyncio
    async def test_create_worker_uses_project_workspace_when_project_id(self, mock_redis, mock_docker):
        """With project_id, should call get_or_create_project_workspace (not create_workspace)."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch(
                "src.manager.workspace_mod.get_or_create_project_workspace",
                return_value=(Path("/tmp/ws/proj-1/workspace"), False),
            ) as mock_proj_ws,
            patch("src.manager.workspace_mod.create_workspace") as mock_worker_ws,
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-1",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        mock_proj_ws.assert_called_once()
        assert mock_proj_ws.call_args[0][1] == "proj-1"
        mock_worker_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_worker_uses_worker_workspace_when_no_project_id(self, mock_redis, mock_docker):
        """Without project_id, should call create_workspace (not get_or_create_project_workspace)."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch(
                "src.manager.workspace_mod.get_or_create_project_workspace",
            ) as mock_proj_ws,
            patch(
                "src.manager.workspace_mod.create_workspace",
                return_value=Path("/tmp/ws/w-2/workspace"),
            ) as mock_worker_ws,
            patch(
                "src.manager.workspace_mod.get_workspace_host_path",
                return_value="/tmp/ws/w-2/workspace",
            ),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-2",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id=None,
            )

        mock_worker_ws.assert_called_once()
        assert mock_worker_ws.call_args[0][1] == "w-2"
        mock_proj_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_reuse_workspace_calls_refresh_token_not_clone(self, mock_redis, mock_docker):
        """When workspace already existed, should refresh git token instead of cloning."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch(
                "src.manager.workspace_mod.get_or_create_project_workspace",
                return_value=(Path("/tmp/ws/proj-1/workspace"), True),  # already_existed=True
            ),
        ):
            manager._setup_git_repo = AsyncMock(return_value=True)
            manager._refresh_git_token = AsyncMock(return_value=True)

            await manager.create_worker_with_capabilities(
                worker_id="w-3",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                env_vars={"REPO_NAME": "org/repo", "GITHUB_TOKEN": "ghp_test"},
            )

        manager._refresh_git_token.assert_awaited_once()
        manager._setup_git_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_workspace_calls_clone_not_refresh(self, mock_redis, mock_docker):
        """When workspace is new, should clone repo instead of refreshing token."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch(
                "src.manager.workspace_mod.get_or_create_project_workspace",
                return_value=(Path("/tmp/ws/proj-1/workspace"), False),  # already_existed=False
            ),
        ):
            manager._setup_git_repo = AsyncMock(return_value=True)
            manager._refresh_git_token = AsyncMock(return_value=True)

            await manager.create_worker_with_capabilities(
                worker_id="w-4",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                env_vars={"REPO_NAME": "org/repo", "GITHUB_TOKEN": "ghp_test"},
            )

        manager._setup_git_repo.assert_awaited_once()
        manager._refresh_git_token.assert_not_awaited()


class TestProjectIdRedisMeta:
    """Tests for project_id persistence in Redis."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_project_id_saved_to_redis_meta(self, mock_docker):
        """project_id should be written to worker:meta:<worker_id> after creation."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_or_create_project_workspace",
            return_value=(Path("/tmp/ws/proj-1/workspace"), False),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-5",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        meta = await redis.hgetall("worker:meta:w-5")
        assert meta.get("project_id") == "proj-1"

    @pytest.mark.asyncio
    async def test_project_id_added_to_active_projects_set(self, mock_docker):
        """project_id should be added to workspace:active_projects set."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_or_create_project_workspace",
            return_value=(Path("/tmp/ws/proj-1/workspace"), False),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-6",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        members = await redis.smembers("workspace:active_projects")
        assert "proj-1" in members


class TestDeleteWorkerPreservation:
    """Tests for workspace preservation on delete."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_delete_worker_preserves_project_workspace(self, mock_docker):
        """delete_worker should NOT remove workspace when meta has project_id."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.hset(
            "worker:meta:w-7",
            mapping={
                "dev_network": "dev_proj_w-7",
                "workspace_path": "/tmp/ws/proj-1/workspace",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-7", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with (
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
            patch("src.manager.ComposeRunner") as mock_runner_cls,
        ):
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner

            await manager.delete_worker("w-7")

        mock_rm.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_worker_removes_worker_workspace(self, mock_docker):
        """delete_worker should remove workspace when meta has no project_id."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.hset(
            "worker:meta:w-8",
            mapping={
                "dev_network": "dev_proj_w-8",
                "workspace_path": "/tmp/ws/w-8/workspace",
            },
        )
        await redis.hset("worker:status:w-8", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with (
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
            patch("src.manager.ComposeRunner") as mock_runner_cls,
        ):
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner

            await manager.delete_worker("w-8")

        mock_rm.assert_called_once()


class TestOrphanGCProjectProtection:
    """Tests for orphan GC protecting project workspaces."""

    @pytest.mark.asyncio
    async def test_orphan_gc_skips_active_project_workspaces(self):
        """GC should not remove workspace dirs that match active project IDs."""
        redis = aioredis.FakeRedis(decode_responses=True)
        # Add proj-1 to active projects set
        await redis.sadd("workspace:active_projects", "proj-1")

        docker = _make_docker_mock()
        docker.list_containers = AsyncMock(return_value=[])
        docker.list_networks = AsyncMock(return_value=[])

        manager = WorkerManager(redis=redis, docker_client=docker)

        with (
            # Workspace dir contains "proj-1" (not a known worker, but an active project)
            patch("os.listdir", return_value=["proj-1"]),
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
        ):
            await manager.garbage_collect_orphaned_resources()

        mock_rm.assert_not_called()


# --- Bugfix: srem on delete ---


class TestDeleteWorkerRemovesFromActiveSet:
    """Bug-fix: delete_worker must srem project_id from workspace:active_projects."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_delete_worker_removes_from_active_projects_set(self, mock_docker):
        """After delete_worker, project_id should no longer be in workspace:active_projects."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.sadd("workspace:active_projects", "proj-1")
        await redis.hset(
            "worker:meta:w-9",
            mapping={
                "dev_network": "dev_proj_w-9",
                "workspace_path": "/tmp/ws/proj-1/workspace",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-9", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner

            await manager.delete_worker("w-9")

        members = await redis.smembers("workspace:active_projects")
        assert "proj-1" not in members


# --- Phase 4: Workspace GC by age ---


class TestWorkspaceGC:
    """Tests for garbage_collect_workspaces (phase 4)."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_workspace_gc_removes_old_workspaces(self, mock_docker):
        """Workspaces older than max_age_hours and not active should be removed."""
        import time

        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        old_mtime = time.time() - (25 * 3600)  # 25 hours ago

        mock_stat = MagicMock()
        mock_stat.st_mtime = old_mtime

        with (
            patch("os.listdir", return_value=["old-proj"]),
            patch("src.manager.Path") as mock_path_cls,
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
        ):
            mock_ws_dir = MagicMock()
            mock_ws_dir.stat.return_value = mock_stat
            mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_ws_dir)

            await manager.garbage_collect_workspaces(max_age_hours=24)

        mock_rm.assert_called_once_with(settings.WORKSPACE_BASE_PATH, "old-proj")

    @pytest.mark.asyncio
    async def test_workspace_gc_preserves_active_workspaces(self, mock_docker):
        """Workspaces in active_projects set should not be removed regardless of age."""
        import time

        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.sadd("workspace:active_projects", "active-proj")
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        old_mtime = time.time() - (48 * 3600)  # 48 hours ago

        mock_stat = MagicMock()
        mock_stat.st_mtime = old_mtime

        with (
            patch("os.listdir", return_value=["active-proj"]),
            patch("src.manager.Path") as mock_path_cls,
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
        ):
            mock_ws_dir = MagicMock()
            mock_ws_dir.stat.return_value = mock_stat
            mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_ws_dir)

            await manager.garbage_collect_workspaces(max_age_hours=24)

        mock_rm.assert_not_called()

    @pytest.mark.asyncio
    async def test_workspace_gc_preserves_recent_workspaces(self, mock_docker):
        """Workspaces younger than max_age_hours should not be removed."""
        import time

        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        recent_mtime = time.time() - (2 * 3600)  # 2 hours ago

        mock_stat = MagicMock()
        mock_stat.st_mtime = recent_mtime

        with (
            patch("os.listdir", return_value=["recent-proj"]),
            patch("src.manager.Path") as mock_path_cls,
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
        ):
            mock_ws_dir = MagicMock()
            mock_ws_dir.stat.return_value = mock_stat
            mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_ws_dir)

            await manager.garbage_collect_workspaces(max_age_hours=24)

        mock_rm.assert_not_called()


# --- Phase 5: Project mutex ---


class TestProjectMutex:
    """Tests for project lock / mutex (phase 5)."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_project_lock_prevents_second_worker(self, mock_docker):
        """Creating a second worker for the same project_id should raise RuntimeError."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_or_create_project_workspace",
            return_value=(Path("/tmp/ws/proj-1/workspace"), False),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-first",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        with (
            patch(
                "src.manager.workspace_mod.get_or_create_project_workspace",
                return_value=(Path("/tmp/ws/proj-1/workspace"), True),
            ),
            pytest.raises(RuntimeError, match="already has active worker"),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-second",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

    @pytest.mark.asyncio
    async def test_project_lock_allows_after_delete(self, mock_docker):
        """After deleting a worker, a new worker for the same project should be allowed."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_or_create_project_workspace",
            return_value=(Path("/tmp/ws/proj-1/workspace"), False),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-first",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner
            await manager.delete_worker("w-first")

        with patch(
            "src.manager.workspace_mod.get_or_create_project_workspace",
            return_value=(Path("/tmp/ws/proj-1/workspace"), True),
        ):
            # Should not raise
            result = await manager.create_worker_with_capabilities(
                worker_id="w-second",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )
            assert result == "w-second"


# --- Phase 6: Failure counter + force clean + retry limit ---


class TestDeleteWorkerCommandReason:
    """Tests for reason field in DeleteWorkerCommand (6.0)."""

    def test_delete_command_accepts_reason(self):
        """DeleteWorkerCommand should accept an optional reason field."""
        cmd = DeleteWorkerCommand(
            request_id="req-1",
            worker_id="w-1",
            reason="failed",
        )
        assert cmd.reason == "failed"

    def test_delete_command_reason_optional(self):
        """DeleteWorkerCommand without reason should default to None."""
        cmd = DeleteWorkerCommand(
            request_id="req-1",
            worker_id="w-1",
        )
        assert cmd.reason is None


class TestFailureCounter:
    """Tests for failure counter in delete_worker (6.1)."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_failure_count_incremented_on_failed(self, mock_docker):
        """delete_worker with reason='failed' should increment failure counter."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.hset(
            "worker:meta:w-10",
            mapping={
                "dev_network": "dev_proj_w-10",
                "workspace_path": "/tmp/ws/proj-1/workspace",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-10", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner
            await manager.delete_worker("w-10", reason="failed")

        count = await redis.get("workspace:proj-1:failure_count")
        assert count == "1"

    @pytest.mark.asyncio
    async def test_failure_count_incremented_on_timeout(self, mock_docker):
        """delete_worker with reason='timeout' should increment failure counter."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.hset(
            "worker:meta:w-11",
            mapping={
                "dev_network": "dev_proj_w-11",
                "workspace_path": "/tmp/ws/proj-1/workspace",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-11", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner
            await manager.delete_worker("w-11", reason="timeout")

        count = await redis.get("workspace:proj-1:failure_count")
        assert count == "1"

    @pytest.mark.asyncio
    async def test_failure_count_reset_on_success(self, mock_docker):
        """delete_worker with reason='completed' should reset failure counter."""
        redis = aioredis.FakeRedis(decode_responses=True)
        # Pre-set failure count
        await redis.set("workspace:proj-1:failure_count", "2")
        await redis.hset(
            "worker:meta:w-12",
            mapping={
                "dev_network": "dev_proj_w-12",
                "workspace_path": "/tmp/ws/proj-1/workspace",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-12", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner
            await manager.delete_worker("w-12", reason="completed")

        count = await redis.get("workspace:proj-1:failure_count")
        assert count is None

    @pytest.mark.asyncio
    async def test_failure_count_not_changed_without_reason(self, mock_docker):
        """delete_worker without reason should not touch failure counter."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.set("workspace:proj-1:failure_count", "1")
        await redis.hset(
            "worker:meta:w-13",
            mapping={
                "dev_network": "dev_proj_w-13",
                "workspace_path": "/tmp/ws/proj-1/workspace",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-13", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner
            await manager.delete_worker("w-13")

        count = await redis.get("workspace:proj-1:failure_count")
        assert count == "1"

    @pytest.mark.asyncio
    async def test_failure_count_has_ttl(self, mock_docker):
        """Failure counter should have a TTL to auto-unblock projects."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.hset(
            "worker:meta:w-14",
            mapping={
                "dev_network": "dev_proj_w-14",
                "workspace_path": "/tmp/ws/proj-1/workspace",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-14", mapping={"status": "RUNNING"})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner
            await manager.delete_worker("w-14", reason="failed")

        ttl = await redis.ttl("workspace:proj-1:failure_count")
        assert ttl > 0  # TTL was set
        assert ttl <= 48 * 3600  # At most 48 hours


class TestForceCleanAndReject:
    """Tests for force clean and spawn rejection (6.2)."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.hset = AsyncMock()
        redis.hget = AsyncMock(return_value=None)
        redis.sadd = AsyncMock()
        redis.sismember = AsyncMock(return_value=False)
        return redis

    @pytest.mark.asyncio
    async def test_force_clean_after_two_failures(self, mock_redis, mock_docker):
        """When failure_count=2, workspace should be wiped before creation."""
        mock_redis.get = AsyncMock(return_value="2")
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch(
                "src.manager.workspace_mod.get_or_create_project_workspace",
                return_value=(Path("/tmp/ws/proj-1/workspace"), False),
            ),
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-15",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        mock_rm.assert_called_once_with(settings.WORKSPACE_BASE_PATH, "proj-1")

    @pytest.mark.asyncio
    async def test_spawn_rejected_after_three_failures(self, mock_redis, mock_docker):
        """When failure_count>=3, spawn should be rejected with RuntimeError."""
        mock_redis.get = AsyncMock(return_value="3")
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with pytest.raises(RuntimeError, match="Max retries"):
            await manager.create_worker_with_capabilities(
                worker_id="w-16",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

    @pytest.mark.asyncio
    async def test_reject_before_wipe(self, mock_redis, mock_docker):
        """When failure_count>=3, workspace should NOT be wiped (reject happens first)."""
        mock_redis.get = AsyncMock(return_value="3")
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
            pytest.raises(RuntimeError, match="Max retries"),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-17",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        mock_rm.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_attempt_creates_fresh_workspace(self, mock_redis, mock_docker):
        """When failure_count=0, should create workspace normally without wipe."""
        mock_redis.get = AsyncMock(return_value=None)
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch(
                "src.manager.workspace_mod.get_or_create_project_workspace",
                return_value=(Path("/tmp/ws/proj-1/workspace"), False),
            ),
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-18",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
            )

        mock_rm.assert_not_called()
