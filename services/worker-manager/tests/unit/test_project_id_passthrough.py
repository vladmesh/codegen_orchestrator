"""Tests for project_id passthrough from consumer to manager."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

import httpx
from fakeredis import aioredis

from shared.contracts.dto.worker import WorkerStatus
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


def _make_create_command(project_id: str | None = None, repo_id: str | None = None) -> CreateWorkerCommand:
    """Build a CreateWorkerCommand with optional project_id and repo_id."""
    config = WorkerConfig(
        name="test-worker",
        worker_type="developer",
        agent_type=AgentType.CLAUDE,
        instructions="test instructions",
        allowed_commands=["*"],
        capabilities=[WorkerCapability.GIT],
        project_id=project_id,
        repo_id=repo_id,
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
    cmd = _make_create_command(project_id="proj-123", repo_id="repo-123")
    await consumer._handle_create(cmd)

    consumer.manager.create_worker_with_capabilities.assert_awaited_once()
    call_kwargs = consumer.manager.create_worker_with_capabilities.call_args.kwargs
    assert call_kwargs["project_id"] == "proj-123"


@pytest.mark.asyncio
async def test_consumer_passes_repo_id_to_manager(consumer):
    """repo_id from WorkerConfig should be forwarded to manager."""
    cmd = _make_create_command(project_id="proj-123", repo_id="repo-123")
    await consumer._handle_create(cmd)

    consumer.manager.create_worker_with_capabilities.assert_awaited_once()
    call_kwargs = consumer.manager.create_worker_with_capabilities.call_args.kwargs
    assert call_kwargs["repo_id"] == "repo-123"


@pytest.mark.asyncio
async def test_consumer_passes_none_project_id_when_missing(consumer):
    """When WorkerConfig has no project_id, None should be forwarded."""
    cmd = _make_create_command(repo_id="repo-123")  # project_id defaults to None
    await consumer._handle_create(cmd)

    consumer.manager.create_worker_with_capabilities.assert_awaited_once()
    call_kwargs = consumer.manager.create_worker_with_capabilities.call_args.kwargs
    assert call_kwargs["project_id"] is None


# --- Phase 2: Workspace by repo_id ---


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


class TestWorkspaceByRepoId:
    """Tests for workspace routing based on repo_id."""

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
    async def test_create_worker_uses_scaffolded_workspace_with_repo_id(self, mock_redis, mock_docker):
        """With repo_id, should call get_scaffolded_workspace."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ) as mock_scaffolded_ws:
            await manager.create_worker_with_capabilities(
                worker_id="w-1",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                repo_id="repo-1",
            )

        mock_scaffolded_ws.assert_called_once_with(settings.SCAFFOLDED_WORKSPACE_PATH, "repo-1")

    @pytest.mark.asyncio
    async def test_create_worker_raises_without_repo_id(self, mock_redis, mock_docker):
        """Without repo_id, should raise RuntimeError."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with pytest.raises(RuntimeError, match="repo_id is required"):
            await manager.create_worker_with_capabilities(
                worker_id="w-2",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                repo_id=None,
            )

    @pytest.mark.asyncio
    async def test_create_worker_raises_when_scaffolded_workspace_missing(self, mock_redis, mock_docker):
        """When scaffolded workspace doesn't exist, should raise RuntimeError."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch(
                "src.manager.workspace_mod.get_scaffolded_workspace",
                return_value=(Path("/tmp/ws/repo-missing"), False),
            ),
            pytest.raises(RuntimeError, match="Scaffolded workspace not found"),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-2b",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                repo_id="repo-missing",
            )

    @pytest.mark.asyncio
    async def test_scaffolded_workspace_refreshes_git_token(self, mock_redis, mock_docker):
        """Pre-scaffolded workspace should refresh git token, not clone."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ):
            manager._setup_git_repo = AsyncMock(return_value=True)
            manager._refresh_git_token = AsyncMock(return_value=True)

            await manager.create_worker_with_capabilities(
                worker_id="w-3",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                repo_id="repo-1",
                env_vars={"REPO_NAME": "org/repo", "GITHUB_TOKEN": "ghp_test"},
            )

        manager._refresh_git_token.assert_awaited_once()
        manager._setup_git_repo.assert_not_awaited()


class TestRepoIdRedisMeta:
    """Tests for repo_id persistence in Redis."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_repo_id_saved_to_redis_meta(self, mock_docker):
        """repo_id should be written to worker:meta:<worker_id> after creation."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-5",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                repo_id="repo-1",
            )

        meta = await redis.hgetall("worker:meta:w-5")
        assert meta.get("repo_id") == "repo-1"

    @pytest.mark.asyncio
    async def test_project_id_saved_to_redis_meta(self, mock_docker):
        """project_id should be written to worker:meta:<worker_id> after creation."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-5b",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
            )

        meta = await redis.hgetall("worker:meta:w-5b")
        assert meta.get("project_id") == "proj-1"

    @pytest.mark.asyncio
    async def test_project_id_added_to_active_projects_set(self, mock_docker):
        """project_id should be added to workspace:active_projects set."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-6",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
            )

        members = await redis.smembers("workspace:active_projects")
        assert "proj-1" in members


class TestDeleteWorkerPreservation:
    """Tests for workspace preservation on delete — scaffolded workspaces are never removed."""

    @pytest.fixture
    def mock_docker(self):
        return _make_docker_mock()

    @pytest.mark.asyncio
    async def test_delete_worker_preserves_workspace_with_project_id(self, mock_docker):
        """delete_worker should NOT remove workspace when meta has project_id."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.hset(
            "worker:meta:w-7",
            mapping={
                "dev_network": "dev_proj_w-7",
                "workspace_path": "/tmp/ws/repo-1",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-7", mapping={"status": WorkerStatus.RUNNING})

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
    async def test_delete_worker_preserves_workspace_without_project_id(self, mock_docker):
        """delete_worker should NOT remove workspace even without project_id (scaffolded workspaces are persistent)."""
        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.hset(
            "worker:meta:w-8",
            mapping={
                "dev_network": "dev_proj_w-8",
                "workspace_path": "/tmp/ws/repo-2",
            },
        )
        await redis.hset("worker:status:w-8", mapping={"status": WorkerStatus.RUNNING})

        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with (
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
            patch("src.manager.ComposeRunner") as mock_runner_cls,
        ):
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner

            await manager.delete_worker("w-8")

        mock_rm.assert_not_called()


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
                "workspace_path": "/tmp/ws/repo-1",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-9", mapping={"status": WorkerStatus.RUNNING})

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

        old_mtime = time.time() - (36 * 3600)  # 36 hours ago (> 35h default)

        mock_stat = MagicMock()
        mock_stat.st_mtime = old_mtime

        with (
            patch("os.listdir", return_value=["old-proj"]),
            patch("src.manager.Path") as mock_path_cls,
            patch("src.manager.workspace_mod.remove_workspace") as mock_rm,
            patch.object(manager, "_notify_workspace_deleted", new_callable=AsyncMock),
        ):
            mock_ws_dir = MagicMock()
            mock_ws_dir.stat.return_value = mock_stat
            mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_ws_dir)

            await manager.garbage_collect_workspaces()

        # Only SCAFFOLDED_WORKSPACE_PATH is scanned now (single path)
        assert mock_rm.call_count == 1

    @pytest.mark.asyncio
    async def test_workspace_gc_notifies_api_on_delete(self, mock_docker):
        """GC calls _notify_workspace_deleted for each removed workspace."""
        import time

        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        old_mtime = time.time() - (48 * 3600)

        mock_stat = MagicMock()
        mock_stat.st_mtime = old_mtime

        with (
            patch("os.listdir", return_value=["repo-abc"]),
            patch("src.manager.Path") as mock_path_cls,
            patch("src.manager.workspace_mod.remove_workspace"),
            patch.object(manager, "_notify_workspace_deleted", new_callable=AsyncMock) as mock_notify,
        ):
            mock_ws_dir = MagicMock()
            mock_ws_dir.stat.return_value = mock_stat
            mock_path_cls.return_value.__truediv__ = MagicMock(return_value=mock_ws_dir)

            await manager.garbage_collect_workspaces()

        # Only SCAFFOLDED_WORKSPACE_PATH is scanned now (single path)
        assert mock_notify.call_count == 1
        mock_notify.assert_any_call("repo-abc")

    @pytest.mark.asyncio
    async def test_workspace_gc_preserves_active_workspaces(self, mock_docker):
        """Workspaces with a live worker should not be removed regardless of age."""
        import time

        redis = aioredis.FakeRedis(decode_responses=True)
        await redis.sadd("workspace:active_projects", "active-proj")
        # Must have a worker:meta entry so active_projects isn't cleaned as stale
        await redis.hset("worker:meta:w1", mapping={"project_id": "active-proj"})
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

            await manager.garbage_collect_workspaces()

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

    @pytest.mark.asyncio
    async def test_notify_workspace_deleted_calls_api(self, mock_docker):
        """_notify_workspace_deleted POSTs to the API endpoint."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.manager.httpx.AsyncClient", return_value=mock_client):
            await manager._notify_workspace_deleted("repo-xyz")

        mock_client.post.assert_awaited_once()
        call_url = mock_client.post.call_args[0][0]
        assert "repo-xyz" in call_url
        assert "notify-workspace-deleted" in call_url

    @pytest.mark.asyncio
    async def test_notify_workspace_deleted_handles_errors(self, mock_docker):
        """_notify_workspace_deleted doesn't raise on API errors."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.httpx.AsyncClient",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            # Should not raise
            await manager._notify_workspace_deleted("repo-xyz")


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
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-first",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
            )

        with (
            patch(
                "src.manager.workspace_mod.get_scaffolded_workspace",
                return_value=(Path("/tmp/ws/repo-1"), True),
            ),
            pytest.raises(RuntimeError, match="already has active worker"),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-second",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
            )

    @pytest.mark.asyncio
    async def test_project_lock_allows_after_delete(self, mock_docker):
        """After deleting a worker, a new worker for the same project should be allowed."""
        redis = aioredis.FakeRedis(decode_responses=True)
        manager = WorkerManager(redis=redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-first",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
            )

        with patch("src.manager.ComposeRunner") as mock_runner_cls:
            mock_runner = MagicMock()
            mock_runner.run = AsyncMock(return_value=(0, "", ""))
            mock_runner_cls.return_value = mock_runner
            await manager.delete_worker("w-first")

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ):
            # Should not raise
            result = await manager.create_worker_with_capabilities(
                worker_id="w-second",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
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
                "workspace_path": "/tmp/ws/repo-1",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-10", mapping={"status": WorkerStatus.RUNNING})

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
                "workspace_path": "/tmp/ws/repo-1",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-11", mapping={"status": WorkerStatus.RUNNING})

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
                "workspace_path": "/tmp/ws/repo-1",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-12", mapping={"status": WorkerStatus.RUNNING})

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
                "workspace_path": "/tmp/ws/repo-1",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-13", mapping={"status": WorkerStatus.RUNNING})

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
                "workspace_path": "/tmp/ws/repo-1",
                "project_id": "proj-1",
            },
        )
        await redis.hset("worker:status:w-14", mapping={"status": WorkerStatus.RUNNING})

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
    """Tests for spawn rejection at high failure count (6.2)."""

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
                repo_id="repo-1",
            )

    @pytest.mark.asyncio
    async def test_reject_before_workspace_resolution(self, mock_redis, mock_docker):
        """When failure_count>=3, workspace should NOT be resolved (reject happens first)."""
        mock_redis.get = AsyncMock(return_value="3")
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with (
            patch("src.manager.workspace_mod.get_scaffolded_workspace") as mock_ws,
            pytest.raises(RuntimeError, match="Max retries"),
        ):
            await manager.create_worker_with_capabilities(
                worker_id="w-17",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
            )

        mock_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_attempt_creates_workspace_normally(self, mock_redis, mock_docker):
        """When failure_count=0, should create workspace normally."""
        mock_redis.get = AsyncMock(return_value=None)
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with patch(
            "src.manager.workspace_mod.get_scaffolded_workspace",
            return_value=(Path("/tmp/ws/repo-1"), True),
        ) as mock_ws:
            await manager.create_worker_with_capabilities(
                worker_id="w-18",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                project_id="proj-1",
                repo_id="repo-1",
            )

        mock_ws.assert_called_once_with(settings.SCAFFOLDED_WORKSPACE_PATH, "repo-1")
