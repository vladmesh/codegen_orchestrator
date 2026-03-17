"""Unit tests for repo_id workspace mounting in WorkerConfig and workspace module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.worker import AgentType, WorkerCapability, WorkerConfig
from src.workspace import get_scaffolded_workspace


class TestWorkerConfigRepoId:
    def test_repo_id_field_accepted(self):
        """WorkerConfig should accept repo_id as an optional field."""
        config = WorkerConfig(
            name="worker-1",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="test",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT],
            repo_id="repo-abc123",
        )
        assert config.repo_id == "repo-abc123"

    def test_repo_id_defaults_to_none(self):
        """WorkerConfig without repo_id should default to None."""
        config = WorkerConfig(
            name="worker-1",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="test",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT],
        )
        assert config.repo_id is None

    def test_repo_id_serializes_in_json(self):
        """repo_id should be present in serialized JSON."""
        config = WorkerConfig(
            name="worker-1",
            worker_type="developer",
            agent_type=AgentType.CLAUDE,
            instructions="test",
            allowed_commands=["*"],
            capabilities=[WorkerCapability.GIT],
            repo_id="repo-xyz",
        )
        data = config.model_dump()
        assert data["repo_id"] == "repo-xyz"


class TestGetScaffoldedWorkspace:
    def test_returns_path_and_exists_true_when_dir_exists(self, tmp_path):
        """Existing scaffolded workspace should return (path, True)."""
        ws_dir = tmp_path / "repo-123"
        ws_dir.mkdir()
        (ws_dir / "some-file.py").touch()

        path, exists = get_scaffolded_workspace(str(tmp_path), "repo-123")
        assert path == ws_dir
        assert exists is True

    def test_returns_path_and_exists_false_when_dir_missing(self, tmp_path):
        """Missing scaffolded workspace should return (path, False)."""
        path, exists = get_scaffolded_workspace(str(tmp_path), "repo-456")
        assert path == tmp_path / "repo-456"
        assert exists is False

    def test_path_structure_is_base_slash_repo_id(self, tmp_path):
        """Path should be base_path/repo_id (no nested /workspace/ subdir)."""
        path, _ = get_scaffolded_workspace(str(tmp_path), "repo-abc")
        assert str(path) == str(tmp_path / "repo-abc")


class AsyncIterEmpty:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


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
    r.get = AsyncMock(return_value=None)
    r.hset = AsyncMock()
    r.hgetall = AsyncMock(return_value={})
    r.sismember = AsyncMock(return_value=False)
    r.sadd = AsyncMock()
    r.srem = AsyncMock()
    r.delete = AsyncMock()
    r.scan_iter = MagicMock(return_value=AsyncIterEmpty())
    return r


class TestCreateWorkerWithRepoId:
    @pytest.mark.asyncio
    @patch("src.manager.git_ops.refresh_git_token", new_callable=AsyncMock, return_value=True)
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_repo_id_mounts_scaffolded_workspace(
        self, mock_builder_cls, mock_workspace, mock_refresh, mock_redis, mock_docker
    ):
        """repo_id present → uses get_scaffolded_workspace, skips scaffold phase."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-123"), True)

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager.create_worker_with_capabilities(
            worker_id="w-1",
            capabilities=["git"],
            base_image="worker-base:latest",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            project_id="proj-1",
            repo_id="repo-123",
        )

        mock_workspace.get_scaffolded_workspace.assert_called_once()
        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_repo_id_missing_workspace_raises(self, mock_builder_cls, mock_workspace, mock_redis, mock_docker):
        """repo_id present but dir doesn't exist → RuntimeError."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-999"), False)

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with pytest.raises(RuntimeError, match="Scaffolded workspace not found"):
            await manager.create_worker_with_capabilities(
                worker_id="w-1",
                capabilities=["git"],
                base_image="worker-base:latest",
                env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
                repo_id="repo-999",
            )

    @pytest.mark.asyncio
    @patch("src.manager.git_ops.refresh_git_token", new_callable=AsyncMock, return_value=True)
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_repo_id_stored_in_redis_meta(
        self, mock_builder_cls, mock_workspace, mock_refresh, mock_redis, mock_docker
    ):
        """repo_id should be persisted in worker:meta:{id} Redis hash."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-123"), True)

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager.create_worker_with_capabilities(
            worker_id="w-1",
            capabilities=["git"],
            base_image="worker-base:latest",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            project_id="proj-1",
            repo_id="repo-123",
        )

        mock_redis.hset.assert_any_call("worker:meta:w-1", "repo_id", "repo-123")

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @patch("src.manager.ImageBuilder")
    async def test_no_repo_id_raises_error(self, mock_builder_cls, mock_workspace, mock_redis, mock_docker):
        """No repo_id → RuntimeError (legacy workspace creation removed)."""
        from src.manager import WorkerManager

        mock_builder = MagicMock()
        mock_builder.get_image_tag.return_value = "worker:test"
        mock_builder.generate_dockerfile.return_value = "FROM base"
        mock_builder_cls.return_value = mock_builder

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with pytest.raises(RuntimeError, match="repo_id is required"):
            await manager.create_worker_with_capabilities(
                worker_id="w-1",
                capabilities=["git"],
                base_image="worker-base:latest",
                env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
                project_id="proj-1",
            )
