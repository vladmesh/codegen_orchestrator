"""Unit tests for worker user isolation — verify telegram_id is passed to API."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with patch("src.consumers.engineering.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get_project = AsyncMock(return_value=None)
        with patch("src.consumers.engineering_result_handler.api_client", api):
            yield api


@pytest.fixture
def mock_deploy_api():
    with (
        patch("src.consumers.deploy.api_client") as api,
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
    ):
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.get_project = AsyncMock(return_value=None)
        yield api


class TestEngineeringWorkerPassesTelegramId:
    @pytest.mark.asyncio
    async def test_get_project_receives_telegram_id(self, mock_redis, mock_api):
        """engineering_worker should pass user_id as telegram_id to get_project."""
        mock_api.get_project.return_value = None  # Project not found → early exit

        from src.consumers.engineering import process_engineering_job

        job_data = {
            "task_id": "eng-test",
            "project_id": "proj-1",
            "user_id": "12345",
            "action": "create",
            "callback_stream": "po:input",
        }

        await process_engineering_job(job_data, mock_redis)

        mock_api.get_project.assert_called_once_with("proj-1", telegram_id=12345)

    @pytest.mark.asyncio
    async def test_empty_user_id_no_telegram_id(self, mock_redis, mock_api):
        """Empty user_id should not pass telegram_id (graceful degradation)."""
        mock_api.get_project.return_value = None

        from src.consumers.engineering import process_engineering_job

        job_data = {
            "task_id": "eng-test",
            "project_id": "proj-1",
            "user_id": "",
            "action": "create",
            "callback_stream": "po:input",
        }

        await process_engineering_job(job_data, mock_redis)

        mock_api.get_project.assert_called_once_with("proj-1")


class TestDeployWorkerPassesTelegramId:
    @pytest.mark.asyncio
    async def test_get_project_receives_telegram_id(self, mock_redis, mock_deploy_api):
        """deploy_worker should pass user_id as telegram_id to get_project."""
        mock_deploy_api.get_project.return_value = None

        from src.consumers.deploy import process_deploy_job

        job_data = {
            "task_id": "deploy-test",
            "project_id": "proj-1",
            "user_id": "67890",
            "callback_stream": "po:input",
            "triggered_by": "po",
            "head_sha": "a" * 40,
        }

        await process_deploy_job(job_data, mock_redis)

        mock_deploy_api.get_project.assert_called_once_with("proj-1", telegram_id=67890)
