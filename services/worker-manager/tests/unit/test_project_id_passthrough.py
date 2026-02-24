"""Tests for project_id passthrough from consumer to manager."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from fakeredis import aioredis

from shared.contracts.queues.worker import (
    AgentType,
    CreateWorkerCommand,
    WorkerCapability,
    WorkerConfig,
)

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
    """Consumer with mocked redis and manager."""
    redis = MagicMock()
    redis.xadd = AsyncMock()
    manager = MagicMock()
    manager.create_worker_with_capabilities = AsyncMock(return_value="test-worker")
    return WorkerCommandConsumer(redis=redis, manager=manager)


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
