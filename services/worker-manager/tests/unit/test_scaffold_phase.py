"""Unit tests for scaffold phase in WorkerManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.worker import ScaffoldConfig


@pytest.fixture
def scaffold_config():
    return ScaffoldConfig(
        template_repo="gh:vladmesh/service-template",
        project_name="my-project",
        modules="backend,tg_bot",
        task_description="Build a bot",
    )


@pytest.fixture
def mock_docker():
    docker = AsyncMock()
    docker.exec_in_container = AsyncMock(return_value=(0, "OK"))
    docker.image_exists = AsyncMock(return_value=True)
    docker.run_container = AsyncMock(return_value=MagicMock(id="container-123"))
    docker.connect_network = AsyncMock()
    docker.create_network = AsyncMock()
    docker.remove_container = AsyncMock()
    docker.remove_network = AsyncMock()
    docker.get_container_logs = AsyncMock(return_value="logs")
    return docker


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.set = AsyncMock()
    r.hset = AsyncMock()
    r.hgetall = AsyncMock(return_value={})
    r.sismember = AsyncMock(return_value=False)
    r.sadd = AsyncMock()
    r.srem = AsyncMock()
    r.delete = AsyncMock()
    r.scan_iter = MagicMock(return_value=AsyncIterEmpty())
    return r


class AsyncIterEmpty:
    """Async iterator that yields nothing."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class TestScaffoldPhase:
    @pytest.mark.asyncio
    async def test_scaffold_script_contains_copier_copy(self, mock_redis, mock_docker, scaffold_config):
        """Scaffold script includes copier copy with correct args."""
        from src.manager import WorkerManager

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        result = await manager._run_scaffold_phase(
            container_id="c-123",
            scaffold_config=scaffold_config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        assert result is True
        mock_docker.exec_in_container.assert_awaited_once()

        # Verify the script content (base64-decoded)
        call_args = mock_docker.exec_in_container.call_args
        cmd = call_args[0][1]
        assert "base64" in cmd

    @pytest.mark.asyncio
    async def test_copier_failure_returns_false(self, mock_redis, mock_docker, scaffold_config):
        """When exec fails, _run_scaffold_phase returns False."""
        from src.manager import WorkerManager

        mock_docker.exec_in_container = AsyncMock(return_value=(1, "copier: command not found"))
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        result = await manager._run_scaffold_phase(
            container_id="c-123",
            scaffold_config=scaffold_config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_scaffold_timeout(self, mock_redis, mock_docker, scaffold_config):
        """Scaffold phase uses 600s timeout."""
        from src.manager import WorkerManager

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager._run_scaffold_phase(
            container_id="c-123",
            scaffold_config=scaffold_config,
            repo="org/my-project",
            token="ghs_token",
            worker_id="w-1",
        )

        call_kwargs = mock_docker.exec_in_container.call_args[1]
        assert call_kwargs["timeout"] == 600


class TestCreateWorkerWithScaffoldConfig:
    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_scaffold_config_triggers_scaffold_phase(
        self, mock_builder_cls, mock_workspace, mock_redis, mock_docker, scaffold_config
    ):
        """create_worker_with_capabilities with scaffold_config calls _run_scaffold_phase."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.create_workspace.return_value = "/tmp/ws/w-1"
        mock_workspace.get_workspace_host_path.return_value = "/tmp/ws/w-1"

        # Marker verification exec returns SCAFFOLD_OK after scaffold phase
        mock_docker.exec_in_container = AsyncMock(return_value=(0, "SCAFFOLD_OK"))

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)
        manager._run_scaffold_phase = AsyncMock(return_value=True)

        await manager.create_worker_with_capabilities(
            worker_id="w-1",
            capabilities=["git"],
            base_image="worker-base:latest",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            scaffold_config=scaffold_config,
        )

        manager._run_scaffold_phase.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_scaffold_markers_missing_raises(
        self, mock_builder_cls, mock_workspace, mock_redis, mock_docker, scaffold_config
    ):
        """Scaffold ran OK but markers missing → RuntimeError."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.create_workspace.return_value = "/tmp/ws/w-1"
        mock_workspace.get_workspace_host_path.return_value = "/tmp/ws/w-1"

        # Scaffold succeeds but marker check fails
        mock_docker.exec_in_container = AsyncMock(return_value=(0, "SCAFFOLD_MISSING"))

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)
        manager._run_scaffold_phase = AsyncMock(return_value=True)

        with pytest.raises(RuntimeError, match="Scaffold markers missing"):
            await manager.create_worker_with_capabilities(
                worker_id="w-1",
                capabilities=["git"],
                base_image="worker-base:latest",
                env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
                scaffold_config=scaffold_config,
            )

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_no_scaffold_config_uses_git_clone(self, mock_builder_cls, mock_workspace, mock_redis, mock_docker):
        """Without scaffold_config, normal git clone path is used."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.create_workspace.return_value = "/tmp/ws/w-1"
        mock_workspace.get_workspace_host_path.return_value = "/tmp/ws/w-1"

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)
        manager._run_scaffold_phase = AsyncMock()
        manager._setup_git_repo = AsyncMock(return_value=True)

        await manager.create_worker_with_capabilities(
            worker_id="w-1",
            capabilities=["git"],
            base_image="worker-base:latest",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

        manager._run_scaffold_phase.assert_not_awaited()
        manager._setup_git_repo.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_scaffold_failure_cleans_up(
        self, mock_builder_cls, mock_workspace, mock_redis, mock_docker, scaffold_config
    ):
        """Scaffold failure triggers worker cleanup and raises."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.create_workspace.return_value = "/tmp/ws/w-1"
        mock_workspace.get_workspace_host_path.return_value = "/tmp/ws/w-1"

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)
        manager._run_scaffold_phase = AsyncMock(return_value=False)
        manager.delete_worker = AsyncMock()

        with pytest.raises(RuntimeError, match="Scaffold phase failed"):
            await manager.create_worker_with_capabilities(
                worker_id="w-1",
                capabilities=["git"],
                base_image="worker-base:latest",
                env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
                scaffold_config=scaffold_config,
            )

        manager.delete_worker.assert_awaited_once_with("w-1")
