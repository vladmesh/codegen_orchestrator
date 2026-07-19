"""Tests for scaffolder consumer."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from src.consumer import _begin_scaffold_work, _finish_scaffold_work, process_scaffold_job
from src.scaffold import ScaffoldResult


def _make_project(**overrides) -> ProjectDTO:
    """Build a ProjectDTO for tests."""
    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "title": "My Project",
        "slug": "my-project",
        "status": "draft",
        "owner_id": 1,
        "config": {},
        "created_by": "system",
        "created_at": "2026-03-17T00:00:00Z",
        "updated_at": "2026-03-17T00:00:00Z",
    }
    base.update(overrides)
    return ProjectDTO.model_validate(base)


# Shared env dict for tests needing GITHUB_ORG
_GITHUB_ENV = {"GITHUB_ORG": "project-factory-organization"}


@pytest.fixture
def valid_job_data():
    return {
        "project_id": "proj-123",
        "repository_id": "repo-456",
        "user_id": "user-1",
        "template_repo": "gh:vladmesh/service-template",
        "template_ref": "0.3.0",
        "project_name": "my-project",
        "modules": "backend,tg_bot",
        "task_description": "Build a string reverser bot",
    }


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.redis.exists.return_value = 0
    redis.redis.eval.return_value = 1
    return redis


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.get_project.return_value = _make_project()
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
    async def test_concurrent_leases_are_released_per_execution(self, mock_redis):
        mock_redis.redis.eval.side_effect = [1, 1]

        first = await _begin_scaffold_work(mock_redis, "proj-123")
        second = await _begin_scaffold_work(mock_redis, "proj-123")

        assert first and second and first != second
        await _finish_scaffold_work(mock_redis, "proj-123", first)
        mock_redis.redis.zrem.assert_awaited_once_with("live:scaffold:leases:proj-123", first)
        assert second != first

    @pytest.mark.asyncio
    async def test_cancelled_registration_does_not_publish_lease(self, mock_redis):
        mock_redis.redis.eval.return_value = 0

        assert await _begin_scaffold_work(mock_redis, "proj-123") is None

    @pytest.mark.asyncio
    async def test_cancel_fence_skips_external_work(self, valid_job_data, mock_redis, mock_github):
        mock_redis.redis.eval.return_value = 0

        with patch("src.consumer.get_github_client", return_value=mock_github):
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result == {"status": "skipped", "error": "cancelled by live teardown"}
        mock_github.get_org_token.assert_not_awaited()
        mock_github.create_repo.assert_not_awaited()

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

        # Should have set status to active on success (no scaffolding/scaffolded)
        mock_api.update_project_status.assert_called_once_with("proj-123", ProjectStatus.ACTIVE)

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
    async def test_scaffold_failure_leaves_project_as_draft(
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

        # Should NOT touch project status on failure (stays draft)
        mock_api.update_project_status.assert_not_called()

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

    @pytest.mark.asyncio
    async def test_branch_protection_called_on_success(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Branch protection should be set after successful scaffold."""
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_github.update_branch_protection.assert_called_once_with(
            "project-factory-organization",
            "my-project",
            "main",
            required_checks=["lint-and-test"],
            require_pr=True,
        )

    @pytest.mark.asyncio
    async def test_branch_protection_failure_does_not_block_scaffold(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Scaffold succeeds even if branch protection fails."""
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")
        mock_github.update_branch_protection.side_effect = RuntimeError("API error")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_api.update_project_status.assert_called_once_with("proj-123", ProjectStatus.ACTIVE)

    @pytest.mark.asyncio
    async def test_branch_protection_not_called_on_failure(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Branch protection should NOT be called when scaffold fails."""
        scaffold_result = ScaffoldResult(success=False, error="copier crashed")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        mock_github.update_branch_protection.assert_not_called()


class TestProcessScaffoldJobEnsureMode:
    """Tests for mode=ensure path in consumer."""

    @pytest.fixture
    def ensure_job_data(self, valid_job_data):
        return {**valid_job_data, "mode": "ensure"}

    @pytest.mark.asyncio
    async def test_ensure_calls_run_ensure_workspace(
        self, ensure_job_data, mock_redis, mock_api, mock_github
    ):
        """mode=ensure should call run_ensure_workspace, not run_scaffold."""
        ensure_result = ScaffoldResult(success=True, tree=".\n-- src")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_ensure_workspace", return_value=ensure_result) as mock_ensure,
            patch("src.consumer.run_scaffold") as mock_full,
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(ensure_job_data, mock_redis)

        assert result["status"] == "success"
        mock_ensure.assert_called_once()
        mock_full.assert_not_called()
        # Should NOT change project status (project is already ACTIVE)
        mock_api.update_project_status.assert_not_called()
        # Should update config with workspace_ready
        mock_api.update_project_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_skipped_returns_skipped_status(
        self, ensure_job_data, mock_redis, mock_api, mock_github
    ):
        """mode=ensure with existing workspace → status=skipped."""
        ensure_result = ScaffoldResult(success=True, skipped=True)

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_ensure_workspace", return_value=ensure_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(ensure_job_data, mock_redis)

        assert result["status"] == "skipped"
        # Should NOT update config when skipped
        mock_api.update_project_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_mode_calls_run_scaffold(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Default mode (full) should call run_scaffold, not run_ensure_workspace."""
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.get_github_client", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result) as mock_full,
            patch("src.consumer.run_ensure_workspace") as mock_ensure,
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(
                os.environ,
                {
                    **_GITHUB_ENV,
                    "ORCHESTRATOR_HOSTNAME": "registry.example.com",
                    "REGISTRY_USER": "admin",
                    "REGISTRY_PASSWORD": "secret",
                },
            ),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_full.assert_called_once()
        mock_ensure.assert_not_called()
        # Full mode SHOULD set project status to ACTIVE
        mock_api.update_project_status.assert_called_once_with("proj-123", ProjectStatus.ACTIVE)
