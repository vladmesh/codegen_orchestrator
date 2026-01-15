"""Unit tests for scaffolder job processing."""

from unittest.mock import AsyncMock, patch

import pytest


class TestProcessJob:
    """Tests for process_job function."""

    @pytest.fixture
    def mock_redis(self):
        """Create mock Redis client."""
        redis = AsyncMock()
        redis.xadd = AsyncMock()
        return redis

    @pytest.fixture
    def valid_job_data(self):
        """Valid job data matching ScaffolderMessage schema."""
        return {
            "request_id": "req-123",
            "project_id": "proj-456",
            "project_name": "my-test-project",
            "repo_full_name": "vladmesh/my-test-project",
            "modules": ["backend"],
        }

    @pytest.mark.asyncio
    async def test_valid_message_parsed_correctly(self, mock_redis, valid_job_data):
        """Job data should be parsed into ScaffolderMessage DTO."""
        with patch("main.scaffold_project", new_callable=AsyncMock) as mock_scaffold:
            mock_scaffold.return_value = True
            with patch("main.update_project_status", new_callable=AsyncMock):
                # Import here to allow patching
                from main import process_job

                await process_job(valid_job_data, mock_redis)

                # Verify scaffold_project received correct args
                mock_scaffold.assert_called_once()
                call_args = mock_scaffold.call_args[0]
                assert call_args[0] == "vladmesh/my-test-project"  # repo_full_name
                assert call_args[1] == "my-test-project"  # project_name
                assert call_args[2] == "proj-456"  # project_id
                assert call_args[3] == "backend"  # modules

    @pytest.mark.asyncio
    async def test_publishes_result_on_success(self, mock_redis, valid_job_data):
        """Successful scaffolding should publish ScaffolderResult with success=True."""
        with patch("main.scaffold_project", new_callable=AsyncMock) as mock_scaffold:
            mock_scaffold.return_value = True
            with patch("main.update_project_status", new_callable=AsyncMock):
                from main import process_job

                await process_job(valid_job_data, mock_redis)

                # Verify result was published
                mock_redis.xadd.assert_called_once()
                call_args = mock_redis.xadd.call_args
                assert call_args[0][0] == "scaffolder:results"

                # Parse published result
                import json

                published_data = json.loads(call_args[0][1]["data"])
                assert published_data["status"] == "success"
                assert published_data["project_id"] == "proj-456"
                assert "github.com/vladmesh/my-test-project" in published_data["repo_url"]

    @pytest.mark.asyncio
    async def test_publishes_failure_result_on_error(self, mock_redis, valid_job_data):
        """Failed scaffolding should publish ScaffolderResult with success=False."""
        with patch("main.scaffold_project", new_callable=AsyncMock) as mock_scaffold:
            mock_scaffold.return_value = False
            with patch("main.update_project_status", new_callable=AsyncMock):
                from main import process_job

                await process_job(valid_job_data, mock_redis)

                # Verify result was published with failure
                mock_redis.xadd.assert_called_once()
                call_args = mock_redis.xadd.call_args

                import json

                published_data = json.loads(call_args[0][1]["data"])
                assert published_data["status"] == "failed"
                assert published_data["error"] is not None

    @pytest.mark.asyncio
    async def test_invalid_data_logs_error(self, mock_redis):
        """Invalid job data should log error and return early."""
        invalid_data = {"project_id": "missing-fields"}

        with patch("main.scaffold_project", new_callable=AsyncMock) as mock_scaffold:
            from main import process_job

            await process_job(invalid_data, mock_redis)

            # scaffold_project should NOT be called for invalid data
            mock_scaffold.assert_not_called()
            # No result should be published
            mock_redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_modules_converted_to_comma_separated(self, mock_redis):
        """Multiple modules should be joined with commas for copier."""
        job_data = {
            "request_id": "req-123",
            "project_id": "proj-456",
            "project_name": "multi-module",
            "repo_full_name": "vladmesh/multi-module",
            "modules": ["backend", "frontend", "telegram"],
        }

        with patch("main.scaffold_project", new_callable=AsyncMock) as mock_scaffold:
            mock_scaffold.return_value = True
            with patch("main.update_project_status", new_callable=AsyncMock):
                from main import process_job

                await process_job(job_data, mock_redis)

                # Check modules arg is comma-separated
                call_args = mock_scaffold.call_args[0]
                assert call_args[3] == "backend,frontend,telegram"
