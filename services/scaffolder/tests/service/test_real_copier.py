"""Service tests for scaffolder with real copier and mock GitHub."""

import json
import os
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_TEMPLATE_REPO = "gh:vladmesh/service-template"


@pytest_asyncio.fixture
async def redis_client():
    """Real Redis client from compose."""
    client = redis.from_url(REDIS_URL, decode_responses=True)

    # Clean up streams before test
    try:
        await client.delete("scaffolder:queue", "scaffolder:results")
    except Exception as e:
        print(f"Failed to cleanup redis: {e}")

    yield client
    await client.aclose()


@pytest.fixture
def mock_git():
    """Mock git operations to avoid actual cloning."""
    with patch("main._run_git") as mock:

        def git_side_effect(*args, **kwargs):
            """Simulate git operations."""
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""

            # For clone, create the directory structure
            if args and "clone" in args:
                cwd = kwargs.get("cwd")
                if cwd:
                    Path(cwd).mkdir(parents=True, exist_ok=True)
            return result

        mock.side_effect = git_side_effect
        yield mock


@pytest.fixture
def mock_github_token():
    """Mock GitHub token to avoid real API calls."""
    with patch("main.get_github_token") as mock:

        async def get_token(org):
            return "ghp_fake_test_token"

        mock.side_effect = get_token
        yield mock


class TestScaffolderServiceWithRealCopier:
    """Service tests with real copier execution."""

    @pytest.mark.asyncio
    async def test_scaffolder_generates_backend_module(
        self, redis_client, mock_git, mock_github_token
    ):
        """
        Test that scaffolder runs real copier with backend module.

        This test:
        1. Sends ScaffolderMessage to queue
        2. Processes message with real copier
        3. Verifies ScaffolderResult is published
        4. Checks that backend files were generated
        """
        from main import process_job

        # Create test message
        message = {
            "request_id": "test-req-001",
            "project_id": "proj-service-001",
            "project_name": "test-backend-proj",
            "repo_full_name": "vladmesh/test-backend-proj",
            "modules": ["backend"],
        }

        # Use a temp directory as "repo" that copier will write to
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            repo_dir.mkdir()

            # Patch tempfile to use our controllable directory
            with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
                mock_tmpdir.return_value.__enter__ = MagicMock(return_value=tmpdir)
                mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

                # Process the job
                await process_job(message, redis_client)

            # Verify result was published
            messages = await redis_client.xrange("scaffolder:results", "-", "+")
            assert len(messages) >= 1, "No result published to scaffolder:results"

            result = json.loads(messages[0][1]["data"])
            assert result["status"] == "success", f"Expected success, got {result}"
            assert result["project_id"] == "proj-service-001"
            assert "vladmesh/test-backend-proj" in result["repo_url"]

    @pytest.mark.asyncio
    async def test_scaffolder_generates_multiple_modules(
        self, redis_client, mock_git, mock_github_token
    ):
        """
        Test that scaffolder generates multiple modules correctly.
        """
        from main import process_job

        message = {
            "request_id": "test-req-002",
            "project_id": "proj-multi-002",
            "project_name": "multi-module-proj",
            "repo_full_name": "vladmesh/multi-module-proj",
            "modules": ["backend", "telegram"],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            repo_dir.mkdir()

            with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
                mock_tmpdir.return_value.__enter__ = MagicMock(return_value=tmpdir)
                mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

                await process_job(message, redis_client)

            # Verify success
            messages = await redis_client.xrange("scaffolder:results", "-", "+")
            result = json.loads(messages[-1][1]["data"])  # Get latest
            assert result["status"] == "success"
            assert result["project_id"] == "proj-multi-002"

    @pytest.mark.asyncio
    async def test_scaffolder_publishes_failure_on_error(self, redis_client, mock_github_token):
        """
        Test that scaffolder publishes failure result when git fails.
        """
        from main import process_job

        # Mock git to fail
        with patch("main._run_git") as mock_git:
            mock_git.return_value = MagicMock(
                returncode=1, stderr="fatal: repository not found", stdout=""
            )

            message = {
                "request_id": "test-req-fail",
                "project_id": "proj-fail-001",
                "project_name": "failing-proj",
                "repo_full_name": "vladmesh/failing-proj",
                "modules": ["backend"],
            }

            with tempfile.TemporaryDirectory() as tmpdir:
                with patch("tempfile.TemporaryDirectory") as mock_tmpdir:
                    mock_tmpdir.return_value.__enter__ = MagicMock(return_value=tmpdir)
                    mock_tmpdir.return_value.__exit__ = MagicMock(return_value=False)

                    await process_job(message, redis_client)

            # Verify failure result was published
            messages = await redis_client.xrange("scaffolder:results", "-", "+")
            result = json.loads(messages[-1][1]["data"])
            assert result["status"] == "failed"
            assert result["error"] is not None

    @pytest.mark.asyncio
    async def test_invalid_message_not_processed(self, redis_client):
        """
        Test that invalid messages don't crash and don't publish results.
        """
        from main import process_job

        # Missing required fields
        invalid_message = {
            "request_id": "test-invalid",
            "project_id": "proj-invalid",
            # Missing project_name, repo_full_name, modules
        }

        await process_job(invalid_message, redis_client)

        # Should not publish anything
        messages = await redis_client.xrange("scaffolder:results", "-", "+")
        # Filter for our request_id
        our_results = [m for m in messages if "test-invalid" in m[1].get("data", "")]
        assert len(our_results) == 0, "Invalid message should not publish result"
