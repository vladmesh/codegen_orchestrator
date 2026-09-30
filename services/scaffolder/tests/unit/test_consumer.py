"""Tests for scaffolder consumer."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.story import WAITING_ON_BY_STATUS, StoryDTO, StoryStatus
from shared.contracts.dto.story_failure import StoryFailureCode
from shared.contracts.dto.task import TaskDTO
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
        "initiating_run_id": "test-run-1",
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
        "telegram_chat_id": "987654321",
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
    gh.get_repo.return_value = MagicMock(allow_auto_merge=True)
    # The consumer enters the client as an async context manager; entering yields
    # the same client and exiting never swallows the operation's exception.
    gh.__aenter__.return_value = gh
    gh.__aexit__.return_value = False
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

        with patch("src.consumer.GitHubAppClient", return_value=mock_github) as client_cls:
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result == {"status": "skipped", "error": "cancelled by live teardown"}
        client_cls.assert_not_called()
        mock_github.__aenter__.assert_not_awaited()
        mock_github.get_org_token.assert_not_awaited()
        mock_github.create_repo.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_updates_status_and_tree(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src\n-- Makefile")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_api.update_project_status.assert_called_once_with("proj-123", ProjectStatus.ACTIVE)

    @pytest.mark.asyncio
    async def test_repo_auto_merge_is_read_back_after_enabling(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")
        mock_github.get_repo.return_value = MagicMock(allow_auto_merge=True)

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_github.enable_repo_auto_merge.assert_awaited_once_with(
            "project-factory-organization", "my-project"
        )
        mock_github.get_repo.assert_awaited_once_with("project-factory-organization", "my-project")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "enable_side_effect, allow_auto_merge",
        [(RuntimeError("GitHub refused auto-merge"), True), (None, False)],
    )
    async def test_repo_auto_merge_verification_failure_is_durable_and_alerted(
        self,
        valid_job_data,
        mock_redis,
        mock_api,
        mock_github,
        enable_side_effect,
        allow_auto_merge,
    ):
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")
        mock_github.enable_repo_auto_merge.side_effect = enable_side_effect
        mock_github.get_repo.return_value = MagicMock(allow_auto_merge=allow_auto_merge)

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch("src.consumer.notify_admins_best_effort", new_callable=AsyncMock) as notify,
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        failure_config = mock_api.update_project_config.await_args_list[-1].args[1]
        assert failure_config["repo_auto_merge_verification"]["status"] == "failed"
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_repo_auto_merge_success_clears_a_prior_failure_mark(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        mock_api.get_project.return_value = _make_project(
            config={"repo_auto_merge_verification": {"status": "failed", "error": "old"}}
        )
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch("src.consumer.run_scaffold", return_value=scaffold_result),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            assert (await process_scaffold_job(valid_job_data, mock_redis))["status"] == "success"

        assert (
            "repo_auto_merge_verification" not in mock_api.update_project_config.await_args.args[1]
        )

    @pytest.mark.asyncio
    async def test_branch_protection_not_called_on_failure(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Branch protection should NOT be called when scaffold fails."""
        scaffold_result = ScaffoldResult(success=False, error="copier crashed")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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
    async def test_ensure_failure_records_scaffold_error_and_keeps_config(
        self, ensure_job_data, mock_redis, mock_api, mock_github
    ):
        """A failed ensure is recorded on the project; nothing else in config changes."""
        mock_api.get_project.return_value = _make_project(
            status="active", config={"modules": ["backend"], "tree": "."}
        )
        ensure_result = ScaffoldResult(success=False, error="Git clone failed: denied")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch("src.consumer.run_ensure_workspace", return_value=ensure_result),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job(ensure_job_data, mock_redis)

        assert result == {"status": "failed", "error": "Git clone failed: denied"}
        mock_api.update_project_config.assert_awaited_once_with(
            "proj-123",
            {"modules": ["backend"], "tree": ".", "scaffold_error": "Git clone failed: denied"},
        )

    @pytest.mark.asyncio
    async def test_ensure_exception_is_recorded_as_scaffold_error(
        self, ensure_job_data, mock_redis, mock_api, mock_github
    ):
        """An exception inside ensure is a failure too, so admission can park on it."""
        mock_api.get_project.return_value = _make_project(status="active", config={})
        mock_github.get_repo.side_effect = RuntimeError("GitHub is unreachable")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch("src.consumer.run_ensure_workspace") as mock_ensure,
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job(ensure_job_data, mock_redis)

        assert result == {"status": "failed", "error": "GitHub is unreachable"}
        mock_ensure.assert_not_called()
        mock_api.update_project_config.assert_awaited_once_with(
            "proj-123", {"scaffold_error": "GitHub is unreachable"}
        )

    @pytest.mark.asyncio
    async def test_full_mode_exception_records_no_scaffold_error(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Full-mode exception behaviour is unchanged: nothing is recorded."""
        mock_github.create_repo.side_effect = RuntimeError("GitHub is unreachable")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        mock_api.update_project_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_full_mode_calls_run_scaffold(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Default mode (full) should call run_scaffold, not run_ensure_workspace."""
        scaffold_result = ScaffoldResult(success=True, tree=".\n-- src")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
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


def _make_story(story_id: str, status: str, reopened_at: str | None = None):
    """Build a StoryDTO for tests, shaped like the response the API returns.

    `waiting_on` is required on the DTO and follows from the status, so the
    fixture derives it rather than pin a constant the status could contradict.
    """
    return StoryDTO.model_validate(
        {
            "id": story_id,
            "project_id": "00000000-0000-0000-0000-000000000001",
            "title": f"story {story_id}",
            "type": "product",
            "status": status,
            "waiting_on": WAITING_ON_BY_STATUS[StoryStatus(status)].value,
            "priority": 0,
            "created_by": "system",
            "reopened_at": reopened_at,
            "created_at": "2026-03-17T00:00:00Z",
            "updated_at": "2026-03-17T00:00:00Z",
        }
    )


def _make_task(
    task_id: str,
    story_id: str,
    created_at: str = "2026-03-17T00:05:00Z",
    status: str = "todo",
) -> TaskDTO:
    return TaskDTO.model_validate(
        {
            "id": task_id,
            "project_id": "00000000-0000-0000-0000-000000000001",
            "type": "feature",
            "title": f"task {task_id}",
            "status": status,
            "priority": 0,
            "current_iteration": 0,
            "max_iterations": 3,
            "created_by": "architect",
            "story_id": story_id,
            "dispatch_admitted": True,
            "created_at": created_at,
            "updated_at": created_at,
        }
    )


async def _run_failed_scaffold(valid_job_data, mock_redis, mock_api, mock_github, error: str):
    with (
        patch("src.consumer.get_api_client", return_value=mock_api),
        patch("src.consumer.GitHubAppClient", return_value=mock_github),
        patch("src.consumer.run_scaffold", return_value=ScaffoldResult(success=False, error=error)),
        patch("src.consumer.get_settings") as mock_settings,
        patch.dict(os.environ, _GITHUB_ENV, clear=False),
        capture_logs() as logs,
    ):
        mock_settings.return_value = MagicMock()
        result = await process_scaffold_job(valid_job_data, mock_redis)
    return result, logs


class TestScaffoldFailureBlastRadius:
    @pytest.mark.asyncio
    async def test_fails_stories_that_only_wait_on_the_scaffold(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """Created and planless in_progress stories fail; work with a plan or past it does not."""
        mock_api.get_stories_by_project.return_value = [
            _make_story("s-created", StoryStatus.CREATED),
            _make_story("s-in-progress-planless", StoryStatus.IN_PROGRESS),
            _make_story("s-in-progress-planned", StoryStatus.IN_PROGRESS),
            _make_story("s-pr-review", StoryStatus.PR_REVIEW),
            _make_story("s-deploying", StoryStatus.DEPLOYING),
            _make_story("s-testing", StoryStatus.TESTING),
            _make_story("s-waiting-human", StoryStatus.WAITING_HUMAN_REVIEW),
            _make_story("s-waiting-secret", StoryStatus.WAITING_USER_SECRET),
            _make_story("s-completed", StoryStatus.COMPLETED),
            _make_story("s-archived", StoryStatus.ARCHIVED),
            _make_story("s-created-2", StoryStatus.CREATED),
        ]
        tasks = {"s-in-progress-planned": [_make_task("t-1", "s-in-progress-planned")]}
        mock_api.get_tasks_by_story.side_effect = lambda story_id: tasks.get(story_id, [])

        result, logs = await _run_failed_scaffold(
            valid_job_data, mock_redis, mock_api, mock_github, "copier crashed"
        )

        assert result["status"] == "failed"
        failed = {call.args[0]: call.args[1] for call in mock_api.fail_story.await_args_list}
        assert list(failed) == ["s-created", "s-in-progress-planless", "s-created-2"]
        for failure in failed.values():
            assert failure.code is StoryFailureCode.SCAFFOLD_FAILED
            assert failure.source == "scaffolder"
            assert failure.detail == "copier crashed"

        summary = next(e for e in logs if e["event"] == "scaffold_stories_failed_summary")
        assert summary["failed_count"] == 3
        assert summary["skipped_count"] == 8

    @pytest.mark.asyncio
    async def test_the_incident_story_taken_by_the_architect_is_failed_with_the_cause(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        """story-3990e41c: in_progress, no tasks, scaffold failed on clone — it must not hide."""
        mock_api.get_stories_by_project.return_value = [
            _make_story("story-3990e41c", StoryStatus.IN_PROGRESS)
        ]
        mock_api.get_tasks_by_story.return_value = []
        error = (
            "Git init/fetch failed: remote: Repository not found.\n"
            "fatal: repository 'https://x-access-token:ghs_secretvalue1234567890@github.com/o/p/' "
            "not found"
        )

        await _run_failed_scaffold(valid_job_data, mock_redis, mock_api, mock_github, error)

        (call,) = mock_api.fail_story.await_args_list
        story_id, failure = call.args
        assert story_id == "story-3990e41c"
        assert "Repository not found" in failure.detail
        assert "ghs_secretvalue" not in failure.detail

    @pytest.mark.asyncio
    async def test_a_reopened_story_with_only_old_cycle_tasks_counts_as_planless(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        mock_api.get_stories_by_project.return_value = [
            _make_story("s-reopened", StoryStatus.IN_PROGRESS, reopened_at="2026-03-18T00:00:00Z")
        ]
        mock_api.get_tasks_by_story.return_value = [
            _make_task("t-old", "s-reopened", created_at="2026-03-17T00:05:00Z", status="done")
        ]

        await _run_failed_scaffold(valid_job_data, mock_redis, mock_api, mock_github, "boom")

        assert [c.args[0] for c in mock_api.fail_story.await_args_list] == ["s-reopened"]

    @pytest.mark.asyncio
    async def test_successful_scaffold_fails_no_story(
        self, valid_job_data, mock_redis, mock_api, mock_github
    ):
        mock_api.get_stories_by_project.return_value = [
            _make_story("s-created", StoryStatus.CREATED),
        ]

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=mock_github),
            patch(
                "src.consumer.run_scaffold",
                return_value=ScaffoldResult(success=True, tree=".\n-- src"),
            ),
            patch("src.consumer.get_settings") as mock_settings,
            patch.dict(os.environ, _GITHUB_ENV, clear=False),
        ):
            mock_settings.return_value = MagicMock()
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_api.fail_story.assert_not_called()


@pytest.fixture
def github_lifecycle(mock_github):
    """A constructed client whose context yields `mock_github`, with one call recorder.

    The recorder orders the context's enter/exit against every GitHub call, so a
    test can prove each call ran on the entered client while its pool was open.
    """
    context = AsyncMock()
    context.__aenter__.return_value = mock_github
    context.__aexit__.return_value = False
    recorder = MagicMock()
    recorder.attach_mock(context, "context")
    recorder.attach_mock(mock_github, "github")
    return context, recorder


def _assert_one_operation_scope(client_cls, recorder, exc_type=None) -> list[str]:
    """One client per operation, entered once, exited once, with every call inside."""
    client_cls.assert_called_once_with()
    # Uses of a call's return value (e.g. truth-testing a repository) are not GitHub calls.
    names = [c[0] for c in recorder.mock_calls if "()" not in c[0]]
    assert names[0] == "context.__aenter__"
    assert names[-1] == "context.__aexit__"
    assert names.count("context.__aenter__") == 1
    assert names.count("context.__aexit__") == 1
    exit_args = recorder.mock_calls[-1].args
    assert (exit_args[0] if exit_args else None) is exc_type
    github_calls = names[1:-1]
    assert all(name.startswith("github.") for name in github_calls)
    return [name.removeprefix("github.") for name in github_calls]


class TestGitHubClientLifecycle:
    """Each admitted scaffold operation owns exactly one GitHub HTTP pool."""

    @pytest.mark.asyncio
    async def test_full_mode_uses_one_entered_client_through_late_read_back(
        self, valid_job_data, mock_redis, mock_api, github_lifecycle
    ):
        context, recorder = github_lifecycle

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=context) as client_cls,
            patch(
                "src.consumer.run_scaffold",
                return_value=ScaffoldResult(success=True, tree=".\n-- src"),
            ),
            patch("src.consumer.get_settings", return_value=MagicMock()),
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
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result == {"status": "success"}
        assert _assert_one_operation_scope(client_cls, recorder) == [
            "get_org_token",
            "create_repo",
            "get_org_token",
            "set_repository_secrets",
            "update_branch_protection",
            "enable_repo_auto_merge",
            "get_repo",
        ]

    @pytest.mark.asyncio
    async def test_ensure_mode_uses_one_entered_client(
        self, valid_job_data, mock_redis, mock_api, github_lifecycle
    ):
        context, recorder = github_lifecycle

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=context) as client_cls,
            patch(
                "src.consumer.run_ensure_workspace",
                return_value=ScaffoldResult(success=True, tree=".\n-- src"),
            ),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job({**valid_job_data, "mode": "ensure"}, mock_redis)

        assert result == {"status": "success"}
        assert _assert_one_operation_scope(client_cls, recorder) == ["get_org_token", "get_repo"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["full", "ensure"])
    async def test_handled_failure_exits_the_context(
        self, valid_job_data, mock_redis, mock_api, github_lifecycle, mode
    ):
        context, recorder = github_lifecycle
        failure = ScaffoldResult(success=False, error="copier crashed")

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=context) as client_cls,
            patch("src.consumer.run_scaffold", return_value=failure),
            patch("src.consumer.run_ensure_workspace", return_value=failure),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job({**valid_job_data, "mode": mode}, mock_redis)

        assert result == {"status": "failed", "error": "copier crashed"}
        _assert_one_operation_scope(client_cls, recorder)
        mock_redis.redis.zrem.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mid_scaffold_github_error_still_exits_the_context(
        self, valid_job_data, mock_redis, mock_api, mock_github, github_lifecycle
    ):
        context, recorder = github_lifecycle
        request = httpx.Request("POST", "https://api.github.com/orgs/org/repos")
        mock_github.create_repo.side_effect = httpx.HTTPStatusError(
            "server error", request=request, response=httpx.Response(502, request=request)
        )

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=context) as client_cls,
            patch("src.consumer.run_scaffold") as run_scaffold,
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(os.environ, _GITHUB_ENV),
        ):
            result = await process_scaffold_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        run_scaffold.assert_not_called()
        assert _assert_one_operation_scope(client_cls, recorder, httpx.HTTPStatusError) == [
            "get_org_token",
            "create_repo",
        ]
        mock_redis.redis.zrem.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancellation_mid_scaffold_exits_the_context(
        self, valid_job_data, mock_redis, mock_api, github_lifecycle
    ):
        context, recorder = github_lifecycle

        with (
            patch("src.consumer.get_api_client", return_value=mock_api),
            patch("src.consumer.GitHubAppClient", return_value=context) as client_cls,
            patch("src.consumer.run_scaffold", side_effect=asyncio.CancelledError),
            patch("src.consumer.get_settings", return_value=MagicMock()),
            patch.dict(
                os.environ,
                {
                    **_GITHUB_ENV,
                    "ORCHESTRATOR_HOSTNAME": "registry.example.com",
                    "REGISTRY_USER": "admin",
                    "REGISTRY_PASSWORD": "secret",
                },
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await process_scaffold_job(valid_job_data, mock_redis)

        assert _assert_one_operation_scope(client_cls, recorder, asyncio.CancelledError) == [
            "get_org_token",
            "create_repo",
            "get_org_token",
            "set_repository_secrets",
        ]
        mock_redis.redis.zrem.assert_awaited_once()
