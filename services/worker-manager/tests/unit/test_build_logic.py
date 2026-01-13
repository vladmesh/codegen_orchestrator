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
from src.image_builder import compute_image_hash


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
        return redis

    @pytest.fixture
    def mock_docker(self):
        docker = MagicMock()
        docker.image_exists = AsyncMock(return_value=False)
        docker.build_image = AsyncMock()
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
        expected_hash = compute_image_hash(["GIT"])
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
        expected_hash = compute_image_hash(["GIT", "CURL"])
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

        assert "FROM worker-base:latest" in dockerfile
        # GIT is pre-installed, but agent type LABEL should be present
        assert "LABEL" in dockerfile
        assert "claude" in dockerfile

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
        expected_hash = compute_image_hash([])
        assert image_tag == f"worker-test:{expected_hash}"


class TestWorkerManagerCreateWithCapabilities:
    """Test create_worker integration with capabilities."""

    @pytest.fixture
    def mock_redis(self):
        redis = MagicMock()
        redis.set = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        return redis

    @pytest.fixture
    def mock_docker(self):
        docker = MagicMock()
        docker.image_exists = AsyncMock(return_value=True)
        docker.build_image = AsyncMock()
        docker.run_container = AsyncMock()
        container = MagicMock()
        container.id = "container-123"
        docker.run_container.return_value = container
        return docker

    @pytest.mark.asyncio
    async def test_create_worker_with_capabilities(self, mock_redis, mock_docker):
        """create_worker should accept capabilities and build image if needed."""
        manager = WorkerManager(redis=mock_redis, docker_client=mock_docker)

        # Now returns worker_id (not container.id) for logical referencing
        result = await manager.create_worker_with_capabilities(
            worker_id="test-worker-1",
            capabilities=["GIT", "CURL"],
            base_image="worker-base:latest",
        )

        assert result == "test-worker-1"  # Returns worker_id for logical reference
        # Should have ensured/built image first
        mock_docker.image_exists.assert_awaited()
        # Should have run container with correct image
        mock_docker.run_container.assert_awaited_once()
        call_kwargs = mock_docker.run_container.call_args[1]
        expected_hash = compute_image_hash(["GIT", "CURL"])
        assert expected_hash in call_kwargs["image"]
