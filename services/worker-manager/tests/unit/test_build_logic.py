"""
Unit tests for P1.5.2 Build Logic.

Tests cover:
- DockerClientWrapper.build_image() method
- WorkerManager.ensure_or_build_image() caching logic
- Cache hit vs cache miss behavior
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.docker_ops import DockerClientWrapper
from src.manager import WorkerManager
from shared.contracts.queues.worker import WorkerOwnership
from src.image_builder import compute_image_hash

# Every worker is created for somebody. These tests are not about who, so they
# use one owner; the tests that are about ownership name their own.
_OWNERSHIP = WorkerOwnership(project_id="proj-test", run_id="eng-test", attempt_id="attempt-eng-test")


BASE_SOURCE_HASH = "basehash0001"


class TestDockerClientWrapperBuild:
    """Test Docker build functionality."""

    @pytest.mark.asyncio
    async def test_build_image_calls_docker_build(self):
        """build_image should call docker client's build method."""
        # Patch docker.from_env to avoid real connection
        with patch("src.docker_ops.docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client

            mock_image = MagicMock()
            mock_image.id = "sha256:abc123"
            mock_image.tags = ["worker:test123"]
            mock_client.images.build.return_value = (mock_image, [])

            wrapper = DockerClientWrapper()

            result = await wrapper.build_image(
                dockerfile_content="FROM python:3.12-slim",
                tag="worker:test123",
            )

            assert result is not None
            mock_client.images.build.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_image_passes_correct_tag(self):
        """build_image should tag the built image correctly."""
        with patch("src.docker_ops.docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client

            mock_image = MagicMock()
            mock_image.id = "sha256:abc123"
            mock_client.images.build.return_value = (mock_image, [])

            wrapper = DockerClientWrapper()

            await wrapper.build_image(
                dockerfile_content="FROM python:3.12-slim",
                tag="worker-test:abc123def456",
            )

            call_kwargs = mock_client.images.build.call_args[1]
            assert call_kwargs["tag"] == "worker-test:abc123def456"


class TestWorkerManagerBuildLogic:
    """Test WorkerManager image building and caching."""

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.hset = AsyncMock()
        redis.hdel = AsyncMock()
        redis.hget = AsyncMock(return_value=None)
        redis.sismember = AsyncMock(return_value=False)
        redis.sadd = AsyncMock()
        redis.srem = AsyncMock()
        redis.delete = AsyncMock()
        redis.xadd = AsyncMock()
        return redis

    @pytest.fixture
    def mock_docker(self):
        docker = MagicMock()
        docker.image_exists = AsyncMock(return_value=False)
        docker.build_image = AsyncMock()
        docker.get_image_label = AsyncMock(return_value=BASE_SOURCE_HASH)
        return docker

    @pytest.mark.asyncio
    async def test_ensure_or_build_image_cache_miss_triggers_build(self, mock_redis, mock_docker):
        """When image doesn't exist, should build it."""
        mock_docker.image_exists.return_value = False

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        image_tag = await manager.ensure_or_build_image(
            capabilities=["GIT"],
            base_image="worker-base:latest",
            prefix="worker-test",
        )

        # Should have checked if image exists
        mock_docker.image_exists.assert_awaited_once()
        mock_docker.build_image.assert_awaited_once()
        # Should return the correct tag
        expected_hash = compute_image_hash(["GIT"], agent_type="claude", source_hash=BASE_SOURCE_HASH)
        assert image_tag == f"worker-test:{expected_hash}"

    @pytest.mark.asyncio
    async def test_ensure_or_build_image_cache_hit_skips_build(self, mock_redis, mock_docker):
        """When image exists, should NOT build it."""
        mock_docker.image_exists.return_value = True

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        image_tag = await manager.ensure_or_build_image(
            capabilities=["GIT", "CURL"],
            base_image="worker-base:latest",
            prefix="worker-test",
            agent_type="claude",
        )

        # Should have checked if image exists
        mock_docker.image_exists.assert_awaited_once()
        # Should NOT have built (cache hit)
        mock_docker.build_image.assert_not_awaited()
        # Should still return correct tag
        expected_hash = compute_image_hash(["GIT", "CURL"], agent_type="claude", source_hash=BASE_SOURCE_HASH)
        assert image_tag == f"worker-test:{expected_hash}"

    @pytest.mark.asyncio
    async def test_ensure_or_build_image_updates_lru(self, mock_redis, mock_docker):
        """Should update LRU timestamp in Redis."""
        mock_docker.image_exists.return_value = True

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager.ensure_or_build_image(
            capabilities=["GIT"],
            base_image="worker-base:latest",
            prefix="worker",
            agent_type="claude",
        )

        # Should update LRU cache
        mock_redis.set.assert_awaited()
        # Check the key pattern
        call_args = mock_redis.set.call_args_list
        lru_calls = [c for c in call_args if "last_used" in str(c)]
        assert len(lru_calls) >= 1

    @pytest.mark.asyncio
    async def test_ensure_or_build_image_generates_correct_dockerfile(self, mock_redis, mock_docker):
        """Build should use correctly generated Dockerfile with agent label."""
        mock_docker.image_exists.return_value = False

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager.ensure_or_build_image(
            capabilities=["GIT"],
            base_image="worker-base:latest",
            prefix="worker-test",
            agent_type="claude",
        )

        # Check dockerfile content passed to build
        call_kwargs = mock_docker.build_image.call_args[1]
        dockerfile = call_kwargs["dockerfile_content"]

        assert "FROM worker-base-claude:latest" in dockerfile
        # GIT is pre-installed, but agent type LABEL should be present
        assert "LABEL" in dockerfile
        assert "claude" in dockerfile

    @pytest.mark.asyncio
    async def test_ensure_or_build_image_tag_tracks_base_source_hash(self, mock_redis, mock_docker):
        """A rebuilt base image must produce a different worker tag, not a stale cache hit."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        mock_docker.get_image_label.return_value = "basehash0001"
        first = await manager.ensure_or_build_image(
            capabilities=["GIT"],
            base_image="worker-base:latest",
            prefix="worker-test",
        )

        mock_docker.get_image_label.return_value = "basehash0002"
        second = await manager.ensure_or_build_image(
            capabilities=["GIT"],
            base_image="worker-base:latest",
            prefix="worker-test",
        )

        assert first != second

    @pytest.mark.asyncio
    async def test_ensure_or_build_image_fails_on_unlabelled_base(self, mock_redis, mock_docker):
        """An unlabelled base image means the make targets never ran — crash instead of caching."""
        mock_docker.get_image_label.return_value = None

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with pytest.raises(RuntimeError, match="worker_source_hash"):
            await manager.ensure_or_build_image(
                capabilities=["GIT"],
                base_image="worker-base:latest",
                prefix="worker-test",
            )

        mock_docker.build_image.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ensure_or_build_image_empty_capabilities(self, mock_redis, mock_docker):
        """Empty capabilities should still work (use base image as-is)."""
        mock_docker.image_exists.return_value = False

        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        image_tag = await manager.ensure_or_build_image(
            capabilities=[],
            base_image="worker-base:latest",
            prefix="worker-test",
            agent_type="claude",
        )

        # Should still build (even if minimal)
        mock_docker.build_image.assert_awaited_once()
        expected_hash = compute_image_hash([], agent_type="claude", source_hash=BASE_SOURCE_HASH)
        assert image_tag == f"worker-test:{expected_hash}"


class TestWorkerManagerCreateWithCapabilities:
    """Test create_worker integration with capabilities."""

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.hset = AsyncMock()
        redis.hdel = AsyncMock()
        redis.hget = AsyncMock(return_value=None)
        redis.sismember = AsyncMock(return_value=False)
        redis.sadd = AsyncMock()
        redis.srem = AsyncMock()
        redis.delete = AsyncMock()
        redis.xadd = AsyncMock()
        return redis

    @pytest.fixture
    def mock_docker(self):
        docker = MagicMock()
        docker.image_exists = AsyncMock(return_value=True)
        docker.build_image = AsyncMock()
        docker.get_image_label = AsyncMock(return_value=BASE_SOURCE_HASH)
        docker.remove_container = AsyncMock()
        docker.create_network = AsyncMock()
        docker.connect_network = AsyncMock()
        docker.run_container = AsyncMock()
        docker.exec_in_container = AsyncMock(return_value=(0, b""))
        container = MagicMock()
        container.id = "container-123"
        docker.run_container.return_value = container
        return docker

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    async def test_create_worker_with_capabilities(self, mock_workspace, mock_redis, mock_docker):
        """create_worker should accept capabilities and build image if needed."""
        from pathlib import Path

        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-1"), True)
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)
        manager._refresh_git_token = AsyncMock(return_value=True)

        result = await manager.create_worker_with_capabilities(
            worker_id="test-worker-1",
            capabilities=["GIT", "CURL"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

        assert result == "test-worker-1"
        mock_docker.image_exists.assert_awaited()
        mock_docker.run_container.assert_awaited_once()
        call_kwargs = mock_docker.run_container.call_args[1]
        expected_hash = compute_image_hash(["GIT", "CURL"], agent_type="claude", source_hash=BASE_SOURCE_HASH)
        assert expected_hash in call_kwargs["image"]

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @pytest.mark.parametrize("auth_mode", ["host_session", "api_key"])
    async def test_factory_worker_forwards_manager_api_key(
        self, mock_workspace, mock_redis, mock_docker, monkeypatch, auth_mode
    ):
        """Factory child containers need FACTORY_API_KEY in every supported auth mode."""
        from pathlib import Path

        monkeypatch.setenv("FACTORY_API_KEY", "fk-test")
        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-1"), True)
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager.create_worker_with_capabilities(
            worker_id="factory-worker-1",
            capabilities=["GIT"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            agent_type="factory",
            auth_mode=auth_mode,
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

        call_kwargs = mock_docker.run_container.call_args[1]
        assert call_kwargs["environment"]["FACTORY_API_KEY"] == "fk-test"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("auth_mode", ["host_session", "api_key"])
    @patch("src.manager.workspace_mod")
    async def test_factory_worker_forwards_explicit_api_key_in_every_auth_mode(
        self, mock_workspace, mock_redis, mock_docker, monkeypatch, auth_mode
    ):
        from pathlib import Path

        monkeypatch.delenv("FACTORY_API_KEY", raising=False)
        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-1"), True)
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        await manager.create_worker_with_capabilities(
            worker_id="factory-worker-explicit-key",
            capabilities=["GIT"],
            base_image="worker-base:latest",
            ownership=_OWNERSHIP,
            agent_type="factory",
            auth_mode=auth_mode,
            api_key="factory-explicit-key",
            repo_id="repo-1",
            env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
        )

        call_kwargs = mock_docker.run_container.call_args[1]
        assert call_kwargs["environment"]["FACTORY_API_KEY"] == "factory-explicit-key"

    @pytest.mark.asyncio
    @patch("src.manager.workspace_mod")
    @pytest.mark.parametrize("auth_mode", ["host_session", "api_key"])
    async def test_factory_worker_fails_fast_without_api_key(
        self, mock_workspace, mock_redis, mock_docker, monkeypatch, auth_mode
    ):
        """Missing Factory credentials fail before launch in every supported auth mode."""
        from pathlib import Path

        monkeypatch.delenv("FACTORY_API_KEY", raising=False)
        mock_workspace.get_scaffolded_workspace.return_value = (Path("/data/workspaces/repo-1"), True)
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        with pytest.raises(RuntimeError, match="FACTORY_API_KEY is not set"):
            await manager.create_worker_with_capabilities(
                worker_id="factory-worker-1",
                capabilities=["GIT"],
                base_image="worker-base:latest",
                ownership=_OWNERSHIP,
                agent_type="factory",
                auth_mode=auth_mode,
                repo_id="repo-1",
                env_vars={"GITHUB_TOKEN": "tok", "REPO_NAME": "org/repo"},
            )

        mock_docker.run_container.assert_not_awaited()
        mock_redis.xadd.assert_not_awaited()
        assert all(call.args[0] != "worker:meta:factory-worker-1" for call in mock_redis.hset.await_args_list)
