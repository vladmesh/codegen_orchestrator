"""Tests for scaffolder consumer."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.consumer import process_scaffold_job
from src.scaffold import ScaffoldResult


@pytest.fixture
def valid_job_data():
    return {
        "project_id": "proj-123",
        "repository_id": "repo-456",
        "user_id": "user-1",
        "template_repo": "/data/service-template",
        "project_name": "my-project",
        "modules": "backend,tg_bot",
        "task_description": "Build a string reverser bot",
    }


@pytest.fixture
def mock_redis():
    return AsyncMock()


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.get_project.return_value = {"id": "proj-123", "config": {}}
    api.get_repository.return_value = {
        "id": "repo-456",
        "git_url": "https://github.com/org/my-project",
        "name": "my-project",
    }
    api.update_project_status.return_value = None
    api.update_project_config.return_value = None
    return api


@pytest.fixture
def mock_github():
    gh = AsyncMock()
    gh.get_org_token.return_value = "ghs_fake"  # noqa: S106
    gh.create_repo.return_value = MagicMock(id=1177997641)
    return gh


class TestProcessScaffoldJob:
    @pytest.mark.asyncio
    async def test_success_updates_status_and_tree(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src\n-- Makefile")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(
                os.environ,
                {
                    "GITHUB_ORG": "project-factory-organization",
                    "ORCHESTRATOR_HOSTNAME": "registry.example.com",
                    "REGISTRY_USER": "admin",
                    "REGISTRY_PASSWORD": "secret",
                },
            ),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"

        # Should have set status to scaffolding then scaffolded
        calls = [c[0] for c in mock_api.update_project_status.call_args_list]
        assert ("proj-123", "scaffolding") in calls
        assert ("proj-123", "scaffolded") in calls

        # Should have updated repository with git_url and provider_repo_id
        mock_api.update_repository.assert_called_once_with(
            "repo-456",
            git_url="https://github.com/project-factory-organization/my-project",
            provider_repo_id=1177997641,
        )

        # Should have set registry secrets for CI build-and-push
        mock_github.set_repository_secrets.assert_called_once_with(
            "project-factory-organization",
            "my-project",
            {
                "REGISTRY_URL": "registry.example.com",
                "REGISTRY_USER": "admin",
                "REGISTRY_PASSWORD": "secret",
            },
            token="ghs_fake",  # noqa: S106
        )

        # Should have saved tree to config
        mock_api.update_project_config.assert_called_once()
        config_call = mock_api.update_project_config.call_args
        assert "tree" in config_call[0][1]

    @pytest.mark.asyncio
    async def test_scaffold_failure_sets_scaffold_failed(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        scaffold_result = ScaffoldResult(success=False, error="copier crashed")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(
                os.environ,
                {
                    "GITHUB_ORG": "project-factory-organization",
                    "ORCHESTRATOR_HOSTNAME": "registry.example.com",
                    "REGISTRY_USER": "admin",
                    "REGISTRY_PASSWORD": "secret",
                },
            ),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        assert "copier crashed" in result["error"]

        # Should have set scaffold_failed
        calls = [c[0] for c in mock_api.update_project_status.call_args_list]
        assert ("proj-123", "scaffold_failed") in calls

    @pytest.mark.asyncio
    async def test_registry_secrets_skipped_when_env_missing(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Scaffold succeeds even without registry env vars — secrets are just skipped."""
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, {"GITHUB_ORG": "test-org"}, clear=False),
        ):
            # Ensure registry vars are NOT set
            os.environ.pop("ORCHESTRATOR_HOSTNAME", None)
            os.environ.pop("REGISTRY_USER", None)
            os.environ.pop("REGISTRY_PASSWORD", None)
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_github.set_repository_secrets.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_message_returns_skipped(self, mock_redis):
        result = await process_scaffold_job({"bad": "data"}, mock_redis)
        assert result["status"] == "skipped"
