"""Unit tests for preparer spawner client."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.clients.preparer_spawner import (
    PREPARER_SPAWN_CHANNEL,
    PreparerRequest,
    PreparerResult,
    request_preparer,
)

# Test constants
DEFAULT_TIMEOUT = 120
CUSTOM_TIMEOUT = 60


class TestPreparerRequest:
    """Tests for PreparerRequest dataclass."""

    def test_create_with_required_fields(self):
        """Test creating request with required fields only."""
        request = PreparerRequest(
            request_id="test-123",
            repo_url="https://github.com/org/repo.git",
            project_name="test_project",
            modules="backend",
            github_token="token123",  # noqa: S106
        )
        assert request.request_id == "test-123"
        assert request.repo_url == "https://github.com/org/repo.git"
        assert request.project_name == "test_project"
        assert request.modules == "backend"
        assert request.github_token == "token123"  # noqa: S105
        # Check defaults
        assert request.task_md == ""
        assert request.agents_md == ""
        assert request.service_template_ref == "main"
        assert request.timeout_seconds == DEFAULT_TIMEOUT

    def test_create_with_all_fields(self):
        """Test creating request with all fields."""
        request = PreparerRequest(
            request_id="test-456",
            repo_url="https://github.com/org/repo.git",
            project_name="my_app",
            modules="backend,tg_bot",
            github_token="token456",  # noqa: S106
            task_md="# Task content",
            agents_md="# Agents content",
            service_template_ref="v2.0",
            timeout_seconds=CUSTOM_TIMEOUT,
        )
        assert request.modules == "backend,tg_bot"
        assert request.task_md == "# Task content"
        assert request.agents_md == "# Agents content"
        assert request.service_template_ref == "v2.0"
        assert request.timeout_seconds == CUSTOM_TIMEOUT


class TestPreparerResult:
    """Tests for PreparerResult dataclass."""

    def test_successful_result(self):
        """Test creating successful result."""
        result = PreparerResult(
            request_id="test-123",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123def",
        )
        assert result.success is True
        assert result.exit_code == 0
        assert result.commit_sha == "abc123def"
        assert result.error_message is None

    def test_failed_result(self):
        """Test creating failed result."""
        result = PreparerResult(
            request_id="test-456",
            success=False,
            exit_code=1,
            output="Error output",
            error_message="Copier failed",
        )
        assert result.success is False
        assert result.exit_code == 1
        assert result.error_message == "Copier failed"
        assert result.commit_sha is None


class TestRequestPreparer:
    """Tests for request_preparer function."""

    def _create_mock_pubsub(self, response_data: dict) -> MagicMock:
        """Create a properly configured mock pubsub."""
        mock_pubsub = MagicMock()

        async def mock_listen():
            yield {
                "type": "message",
                "data": json.dumps(response_data),
            }

        mock_pubsub.listen = mock_listen
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        return mock_pubsub

    def _create_mock_redis_client(self, mock_pubsub: MagicMock) -> MagicMock:
        """Create a properly configured mock Redis client."""
        mock_client = MagicMock()
        mock_client.pubsub.return_value = mock_pubsub
        mock_client.publish = AsyncMock()
        mock_client.aclose = AsyncMock()
        return mock_client

    @pytest.fixture
    def mock_settings(self):
        """Mock settings with Redis URL."""
        with patch("src.clients.preparer_spawner.get_settings") as mock:
            mock.return_value = MagicMock(redis_url="redis://localhost:6379")
            yield mock

    @pytest.fixture
    def mock_redis(self):
        """Mock Redis module."""
        with patch("src.clients.preparer_spawner.redis") as mock:
            yield mock

    @pytest.mark.asyncio
    async def test_request_preparer_success(self, mock_settings, mock_redis):
        """Test successful preparer request."""
        response_data = {
            "request_id": "test-123",
            "success": True,
            "exit_code": 0,
            "output": "Done",
            "commit_sha": "abc123",
        }
        mock_pubsub = self._create_mock_pubsub(response_data)
        mock_client = self._create_mock_redis_client(mock_pubsub)
        mock_redis.from_url.return_value = mock_client

        result = await request_preparer(
            repo_url="https://github.com/org/repo.git",
            project_name="test_project",
            modules=["backend"],
            github_token="token123",  # noqa: S106
        )

        assert isinstance(result, PreparerResult)
        assert result.success is True
        assert result.commit_sha == "abc123"

        # Verify publish was called
        mock_client.publish.assert_called_once()
        call_args = mock_client.publish.call_args
        assert call_args[0][0] == PREPARER_SPAWN_CHANNEL

    @pytest.mark.asyncio
    async def test_request_preparer_failure(self, mock_settings, mock_redis):
        """Test failed preparer request."""
        response_data = {
            "request_id": "test-456",
            "success": False,
            "exit_code": 1,
            "output": "Error",
            "error_message": "Copier failed",
        }
        mock_pubsub = self._create_mock_pubsub(response_data)
        mock_client = self._create_mock_redis_client(mock_pubsub)
        mock_redis.from_url.return_value = mock_client

        result = await request_preparer(
            repo_url="https://github.com/org/repo.git",
            project_name="test_project",
            modules=["backend", "tg_bot"],
            github_token="token456",  # noqa: S106
        )

        assert isinstance(result, PreparerResult)
        assert result.success is False
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_request_preparer_modules_join(self, mock_settings, mock_redis):
        """Test that modules list is joined correctly."""
        response_data = {
            "request_id": "test",
            "success": True,
            "exit_code": 0,
            "output": "Done",
        }
        mock_pubsub = self._create_mock_pubsub(response_data)
        mock_client = self._create_mock_redis_client(mock_pubsub)
        mock_redis.from_url.return_value = mock_client

        await request_preparer(
            repo_url="https://github.com/org/repo.git",
            project_name="test_project",
            modules=["backend", "tg_bot", "notifications"],
            github_token="token",  # noqa: S106
        )

        # Check the published message contains joined modules
        publish_call = mock_client.publish.call_args
        published_data = json.loads(publish_call[0][1])
        assert published_data["modules"] == "backend,tg_bot,notifications"

    @pytest.mark.asyncio
    async def test_request_preparer_with_task_md(self, mock_settings, mock_redis):
        """Test that task_md and agents_md are passed correctly."""
        response_data = {
            "request_id": "test",
            "success": True,
            "exit_code": 0,
            "output": "Done",
        }
        mock_pubsub = self._create_mock_pubsub(response_data)
        mock_client = self._create_mock_redis_client(mock_pubsub)
        mock_redis.from_url.return_value = mock_client

        await request_preparer(
            repo_url="https://github.com/org/repo.git",
            project_name="test_project",
            modules=["backend"],
            github_token="token",  # noqa: S106
            task_md="# Task\nDo stuff",
            agents_md="# Agents\nFollow rules",
        )

        publish_call = mock_client.publish.call_args
        published_data = json.loads(publish_call[0][1])
        assert published_data["task_md"] == "# Task\nDo stuff"
        assert published_data["agents_md"] == "# Agents\nFollow rules"
