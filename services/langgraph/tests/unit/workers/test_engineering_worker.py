"""Unit tests for engineering worker fail-fast checks.

Tests commit_sha gate in _handle_engineering_success and
CI gate fail-closed behavior in _wait_for_ci_and_fix.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient."""
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    """Patch api_client methods used by the engineering worker."""
    with patch("src.workers.engineering_worker.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get_project = AsyncMock(return_value=None)
        yield api


def _project(*, repo_url=None):
    p = {"id": "proj-1", "name": "test-project", "config": {"modules": ["backend"]}}
    if repo_url:
        p["repository_url"] = repo_url
    return p


class TestHandleEngineeringSuccess:
    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_no_commit_sha_fails_fast(self, mock_ci_gate, mock_redis, mock_api):
        """commit_sha=None must return failed, not proceed to CI/deploy."""
        mock_ci_gate.return_value = (True, [])  # Should never be reached

        from src.workers.engineering_worker import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": None,
        }

        out = await _handle_engineering_success(
            result=result_data,
            task_id="eng-1",
            project=_project(repo_url="https://github.com/org/test-project"),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        assert out["status"] == "failed"
        error_lower = out.get("error", "").lower()
        assert "commit_sha" in error_lower or "commit" in error_lower

        # Task must be patched as failed
        mock_api.patch.assert_called()
        patch_calls = [c for c in mock_api.patch.call_args_list if "tasks/" in str(c)]
        assert any("failed" in str(c) for c in patch_calls)

        # Callback must be "failed" (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        failed_events = [c for c in flat_calls if c[0][1].get("event") == "failed"]
        assert len(failed_events) >= 1

        # Deploy queue must NOT have been written to
        xadd_calls = mock_redis.redis.xadd.call_args_list
        deploy_calls = [c for c in xadd_calls if "deploy" in str(c[0][0])]
        assert len(deploy_calls) == 0

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_with_commit_sha_proceeds(self, mock_ci_gate, mock_redis, mock_api):
        """commit_sha present must proceed to CI gate and then deploy."""
        mock_ci_gate.return_value = (True, [])

        from src.workers.engineering_worker import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        out = await _handle_engineering_success(
            result=result_data,
            task_id="eng-1",
            project=_project(repo_url="https://github.com/org/test-project"),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        assert out["status"] == "success"
        assert out["commit_sha"] == "abc123"
        mock_ci_gate.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_deploy_message_includes_user_id(self, mock_ci_gate, mock_redis, mock_api):
        """DeployMessage queued after CI must include user_id (BUG 17)."""
        mock_ci_gate.return_value = (True, [])

        from src.workers.engineering_worker import _handle_engineering_success

        result_data = {"engineering_status": "done", "commit_sha": "abc123"}

        await _handle_engineering_success(
            result=result_data,
            task_id="eng-1",
            project=_project(repo_url="https://github.com/org/test-project"),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="625038902",
        )

        # Find the deploy queue xadd call
        import json

        from shared.queues import DEPLOY_QUEUE

        xadd_calls = mock_redis.redis.xadd.call_args_list
        deploy_calls = [c for c in xadd_calls if c[0][0] == DEPLOY_QUEUE]
        assert len(deploy_calls) == 1, (
            f"Expected 1 deploy queue call, got {len(deploy_calls)}. "
            f"All xadd streams: {[c[0][0] for c in xadd_calls]}"
        )

        deploy_data = json.loads(deploy_calls[0][0][1]["data"])
        assert (
            deploy_data["user_id"] == "625038902"
        ), f"user_id mismatch. Full deploy_data: {deploy_data}"


class TestNotificationDecoupling:
    """Tests that notification type is decoupled from deploy trigger."""

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_ci_passed_sends_progress_when_deploying(
        self, mock_ci_gate, mock_redis, mock_api
    ):
        """skip_deploy=False → event type is 'progress', not 'completed'."""
        mock_ci_gate.return_value = (True, [])

        from src.workers.engineering_worker import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        await _handle_engineering_success(
            result=result_data,
            task_id="eng-1",
            project=_project(repo_url="https://github.com/org/test-project"),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        # Find callback events on the callback stream (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        callback_events = [c for c in flat_calls if c[0][0] == "po:response:abc"]

        # There should be a "progress" event with deploy message
        progress_events = [c for c in callback_events if c[0][1].get("event") == "progress"]
        assert any("deploying" in c[0][1].get("text", "").lower() for c in progress_events)

        # There should NOT be a "completed" event from engineering worker
        completed_events = [c for c in callback_events if c[0][1].get("event") == "completed"]
        assert len(completed_events) == 0

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_ci_passed_sends_completed_when_skip_deploy(
        self, mock_ci_gate, mock_redis, mock_api
    ):
        """skip_deploy=True → event type is 'completed' (this IS the final step)."""
        mock_ci_gate.return_value = (True, [])

        from src.workers.engineering_worker import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        await _handle_engineering_success(
            result=result_data,
            task_id="eng-1",
            project=_project(repo_url="https://github.com/org/test-project"),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        # Find callback events on the callback stream (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        callback_events = [c for c in flat_calls if c[0][0] == "po:response:abc"]

        # There should be a "completed" event
        completed_events = [c for c in callback_events if c[0][1].get("event") == "completed"]
        assert len(completed_events) == 1

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_deploy_trigger_failure_publishes_failed_event(
        self, mock_ci_gate, mock_redis, mock_api
    ):
        """When deploy queuing fails, user gets a 'failed' notification."""
        mock_ci_gate.return_value = (True, [])
        # Make deploy task creation fail
        mock_api.post.side_effect = RuntimeError("API unreachable")

        from src.workers.engineering_worker import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        await _handle_engineering_success(
            result=result_data,
            task_id="eng-1",
            project=_project(repo_url="https://github.com/org/test-project"),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        # Find callback events on the callback stream (via publish_flat)
        flat_calls = mock_redis.publish_flat.call_args_list
        callback_events = [c for c in flat_calls if c[0][0] == "po:response:abc"]

        # There should be a "failed" event about deploy trigger
        failed_events = [c for c in callback_events if c[0][1].get("event") == "failed"]
        assert len(failed_events) >= 1


class TestCIGateFailClosed:
    @pytest.mark.asyncio
    async def test_missing_repo_url_returns_false(self, mock_redis):
        """CI gate must fail-closed (return False) when project has no repository_url."""
        from src.workers.engineering_worker import _wait_for_ci_and_fix

        passed, ci_attempts = await _wait_for_ci_and_fix(
            project={"id": "p1"},
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            user_id="u1",
        )

        assert passed is False
        assert ci_attempts == []

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch("src.workers.engineering_worker._respawn_developer_for_ci_fix", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker.publish_callback_event", new_callable=AsyncMock)
    async def test_ci_retry_uses_pre_respawn_timestamp(
        self, mock_publish, mock_record, mock_respawn, mock_gh_cls, mock_redis
    ):
        """On retry, created_after must be from BEFORE respawn, not after.

        Regression test for BUG 3: if created_after is captured at the top of
        the next iteration (after the respawned developer already pushed),
        the new CI run is invisible → infinite poll.
        """
        from src.workers.engineering_worker import _wait_for_ci_and_fix

        # Track created_after values passed to wait_for_workflow_completion
        captured_timestamps: list[datetime] = []
        respawn_started_at: list[datetime] = []

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh

        # attempt 0: CI fails → RuntimeError with run_id
        # attempt 1: CI passes
        async def fake_wait(**kwargs):
            captured_timestamps.append(kwargs["created_after"])
            if len(captured_timestamps) == 1:
                raise RuntimeError("CI failed (run_id=12345)")
            return {"id": 99, "conclusion": "success"}

        mock_gh.wait_for_workflow_completion = AsyncMock(side_effect=fake_wait)
        mock_gh.get_workflow_failure_logs = AsyncMock(return_value="error log")

        # Respawn records its start timestamp for assertion
        async def fake_respawn(**kwargs):
            respawn_started_at.append(datetime.now(UTC))
            await asyncio.sleep(0)  # yield control without real delay
            return True

        mock_respawn.side_effect = fake_respawn

        passed, ci_attempts = await _wait_for_ci_and_fix(
            project=_project(repo_url="https://github.com/org/repo"),
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            developer_started_at=datetime(2025, 1, 1, tzinfo=UTC),
            user_id="u1",
        )

        expected_attempts = 2  # attempt 0 (initial) + attempt 1 (retry)
        assert passed is True
        assert len(captured_timestamps) == expected_attempts
        assert len(ci_attempts) == expected_attempts
        assert ci_attempts[0]["status"] == "failed"
        assert ci_attempts[1]["status"] == "passed"

        # attempt 0: should use the developer_started_at we passed in
        assert captured_timestamps[0] == datetime(2025, 1, 1, tzinfo=UTC)

        # attempt 1: created_after must be from BEFORE the respawn started
        assert captured_timestamps[1] < respawn_started_at[0]

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch("src.workers.engineering_worker._respawn_developer_for_ci_fix", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker.publish_callback_event", new_callable=AsyncMock)
    async def test_workflow_not_found_returns_false_immediately(
        self, mock_publish, mock_record, mock_respawn, mock_gh_cls, mock_redis
    ):
        """WorkflowNotFoundError must fail-fast without respawning developer."""
        from shared.clients.github import WorkflowNotFoundError
        from src.workers.engineering_worker import _wait_for_ci_and_fix

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh
        mock_gh.wait_for_workflow_completion = AsyncMock(
            side_effect=WorkflowNotFoundError("ci.yml not found in org/repo"),
        )

        passed, ci_attempts = await _wait_for_ci_and_fix(
            project=_project(repo_url="https://github.com/org/repo"),
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            user_id="u1",
        )

        assert passed is False
        assert len(ci_attempts) == 1
        assert ci_attempts[0]["status"] == "workflow_not_found"
        mock_respawn.assert_not_awaited()


class TestCIFailureClassification:
    """Tests for _is_infra_failure classification (BUG 15)."""

    def test_registry_login_is_infra(self):
        from src.workers.engineering_worker import _is_infra_failure

        ctx = (
            "Job 'build-and-push (backend, ., services/backend/Dockerfile, backend)' failed:\n"
            "  Step 'Log in to Docker Registry' failed"
        )
        assert _is_infra_failure(ctx) is True

    def test_docker_login_is_infra(self):
        from src.workers.engineering_worker import _is_infra_failure

        assert _is_infra_failure("docker login failed: connection refused") is True

    def test_tls_handshake_is_infra(self):
        from src.workers.engineering_worker import _is_infra_failure

        assert _is_infra_failure("TLS handshake error on registry:5000") is True

    def test_deploy_step_is_infra(self):
        from src.workers.engineering_worker import _is_infra_failure

        ctx = "Job 'deploy' failed:\n  Step 'Deploy to server via SSH' failed"
        assert _is_infra_failure(ctx) is True

    def test_ruff_lint_is_not_infra(self):
        from src.workers.engineering_worker import _is_infra_failure

        ctx = "Job 'lint-and-test' failed:\n" "  Step 'Run ruff check' failed"
        assert _is_infra_failure(ctx) is False

    def test_pytest_is_not_infra(self):
        from src.workers.engineering_worker import _is_infra_failure

        ctx = "Job 'lint-and-test' failed:\n" "  Step 'Run tests' failed"
        assert _is_infra_failure(ctx) is False

    def test_empty_context_is_not_infra(self):
        from src.workers.engineering_worker import _is_infra_failure

        assert _is_infra_failure("") is False


class TestCIInfraFailFast:
    """Tests that infra CI failures don't respawn developer (BUG 15)."""

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch("src.workers.engineering_worker._respawn_developer_for_ci_fix", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker.publish_callback_event", new_callable=AsyncMock)
    async def test_infra_failure_skips_respawn(
        self, mock_publish, mock_record, mock_respawn, mock_gh_cls, mock_redis
    ):
        """Infra CI failure must return False without spawning a developer."""
        from src.workers.engineering_worker import _wait_for_ci_and_fix

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh

        # CI fails with registry error
        mock_gh.wait_for_workflow_completion = AsyncMock(
            side_effect=RuntimeError(
                "Workflow ci.yml failed: failure. "
                "See: https://github.com/org/repo/actions/runs/12345"
            )
        )
        mock_gh.get_workflow_failure_logs = AsyncMock(
            return_value=(
                "Job 'build-and-push (backend)' failed:\n"
                "  Step 'Log in to Docker Registry' failed"
            )
        )

        passed, ci_attempts = await _wait_for_ci_and_fix(
            project=_project(repo_url="https://github.com/org/repo"),
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            developer_started_at=datetime(2025, 1, 1, tzinfo=UTC),
            user_id="u1",
        )

        assert passed is False
        assert len(ci_attempts) == 1
        assert ci_attempts[0]["status"] == "failed"
        mock_respawn.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch("src.workers.engineering_worker._respawn_developer_for_ci_fix", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.workers.engineering_worker.publish_callback_event", new_callable=AsyncMock)
    async def test_code_failure_does_respawn(
        self, mock_publish, mock_record, mock_respawn, mock_gh_cls, mock_redis
    ):
        """Code CI failure (lint/test) must respawn developer as before."""
        from src.workers.engineering_worker import _wait_for_ci_and_fix

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh

        call_count = 0

        async def fake_wait(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError(
                    "Workflow ci.yml failed: failure. "
                    "See: https://github.com/org/repo/actions/runs/12345"
                )
            return {"id": 99, "conclusion": "success"}

        mock_gh.wait_for_workflow_completion = AsyncMock(side_effect=fake_wait)
        mock_gh.get_workflow_failure_logs = AsyncMock(
            return_value=("Job 'lint-and-test' failed:\n" "  Step 'Run ruff check' failed")
        )
        mock_respawn.return_value = True

        passed, ci_attempts = await _wait_for_ci_and_fix(
            project=_project(repo_url="https://github.com/org/repo"),
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            developer_started_at=datetime(2025, 1, 1, tzinfo=UTC),
            user_id="u1",
        )

        expected_ci_attempts = 2  # 1 failed + 1 passed
        assert passed is True
        assert len(ci_attempts) == expected_ci_attempts
        assert ci_attempts[0]["status"] == "failed"
        assert ci_attempts[1]["status"] == "passed"
        mock_respawn.assert_awaited_once()


class TestCreateRepoAndSetSecrets:
    """Tests for _create_repo_and_set_secrets (replaced _trigger_scaffolding)."""

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch.dict(
        "os.environ",
        {
            "GITHUB_ORG": "test-org",
            "ORCHESTRATOR_HOSTNAME": "registry.example.com",
            "REGISTRY_USER": "admin",
            "REGISTRY_PASSWORD": "secret",
        },
    )
    async def test_happy_path(self, mock_gh_cls, mock_api):
        """Creates repo, sets secrets, updates project status."""
        from src.workers.engineering_worker import _create_repo_and_set_secrets

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh
        mock_gh.create_repo = AsyncMock()
        mock_gh.get_org_token = AsyncMock(return_value="ghs_token")
        mock_gh.set_repository_secrets = AsyncMock(return_value=3)

        project = {"id": "proj-1", "name": "My Project"}

        await _create_repo_and_set_secrets(project)

        # Repo was created
        mock_gh.create_repo.assert_awaited_once_with(
            org="test-org",
            name="my-project",
            description="Project: My Project",
            private=True,
        )

        # Secrets were set
        mock_gh.set_repository_secrets.assert_awaited_once()
        secrets_arg = mock_gh.set_repository_secrets.call_args[0][2]
        assert secrets_arg["REGISTRY_URL"] == "registry.example.com"
        assert secrets_arg["REGISTRY_USER"] == "admin"
        assert secrets_arg["REGISTRY_PASSWORD"] == "secret"  # noqa: S105

        # Project status updated to scaffolding with repo URL
        mock_api.patch.assert_called()
        patch_calls = [c for c in mock_api.patch.call_args_list if "projects/" in str(c)]
        assert any("scaffolding" in str(c) for c in patch_calls)

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch.dict(
        "os.environ",
        {
            "GITHUB_ORG": "test-org",
            "ORCHESTRATOR_HOSTNAME": "registry.example.com",
            "REGISTRY_USER": "admin",
            "REGISTRY_PASSWORD": "secret",
        },
    )
    async def test_repo_already_exists_fails_fast(self, mock_gh_cls, mock_api):
        """Fails fast when repo already exists (stale state from previous run)."""
        from src.workers.engineering_worker import _create_repo_and_set_secrets

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh
        mock_gh.create_repo = AsyncMock(side_effect=Exception("422: already exists"))

        project = {"id": "proj-1", "name": "existing-project"}

        with pytest.raises(RuntimeError, match="already exists"):
            await _create_repo_and_set_secrets(project)

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch.dict("os.environ", {"GITHUB_ORG": "test-org"}, clear=False)
    async def test_missing_registry_env_warns(self, mock_gh_cls, mock_api):
        """Missing registry env vars logs warning but doesn't fail."""
        import os

        from src.workers.engineering_worker import _create_repo_and_set_secrets

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh
        mock_gh.create_repo = AsyncMock()

        # Clear registry env vars
        env = os.environ.copy()
        for key in ("ORCHESTRATOR_HOSTNAME", "REGISTRY_USER", "REGISTRY_PASSWORD"):
            env.pop(key, None)

        project = {"id": "proj-1", "name": "test"}

        with patch.dict("os.environ", env, clear=True):
            await _create_repo_and_set_secrets(project)

        # Repo still created, but set_repository_secrets NOT called
        mock_gh.create_repo.assert_awaited_once()
        mock_gh.set_repository_secrets.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_github_org_raises(self, mock_api):
        """Raises RuntimeError when GITHUB_ORG is not set."""
        from src.workers.engineering_worker import _create_repo_and_set_secrets

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="GITHUB_ORG"):
                await _create_repo_and_set_secrets({"id": "p1", "name": "x"})


class TestRespawnDeveloperForCIFix:
    """Tests for _respawn_developer_for_ci_fix internals."""

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.request_spawn", new_callable=AsyncMock)
    async def test_passes_project_id_to_request_spawn(self, mock_spawn):
        """project_id must be forwarded to request_spawn for workspace persistence."""
        from src.workers.engineering_worker import _respawn_developer_for_ci_fix

        mock_spawn.return_value = AsyncMock(success=True)

        mock_gh = AsyncMock()
        mock_gh.get_token = AsyncMock(return_value="ghp_token")

        project = {"id": "proj-abc123", "name": "my-project"}

        await _respawn_developer_for_ci_fix(
            project=project,
            owner="org",
            repo_name="repo",
            repo_full_name="org/repo",
            github_client=mock_gh,
            failure_context="Step 'Run tests' failed",
            attempt=1,
        )

        mock_spawn.assert_awaited_once()
        _, kwargs = mock_spawn.call_args
        assert kwargs.get("project_id") == "proj-abc123"

    @pytest.mark.asyncio
    @patch("src.clients.worker_spawner.request_spawn", new_callable=AsyncMock)
    async def test_project_id_none_when_missing(self, mock_spawn):
        """project_id=None when project dict has no 'id' key (defensive)."""
        from src.workers.engineering_worker import _respawn_developer_for_ci_fix

        mock_spawn.return_value = AsyncMock(success=True)

        mock_gh = AsyncMock()
        mock_gh.get_token = AsyncMock(return_value="ghp_token")

        project = {"name": "orphan-project"}  # no "id"

        await _respawn_developer_for_ci_fix(
            project=project,
            owner="org",
            repo_name="repo",
            repo_full_name="org/repo",
            github_client=mock_gh,
            failure_context="error",
            attempt=1,
        )

        _, kwargs = mock_spawn.call_args
        assert kwargs.get("project_id") is None
