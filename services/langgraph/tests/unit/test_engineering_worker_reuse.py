"""Tests for engineering worker CI fix loop with worker reuse (Iteration 3: worker-reuse-ci-fix)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.project import ProjectStatus
from src.clients.worker_spawner import SpawnResult


def _project(**overrides):
    """Minimal project dict for tests."""
    base = {
        "id": "proj-1",
        "name": "test-project",
        "config": {"modules": ["backend"]},
    }
    base.update(overrides)
    return base


# ---------- 3.1: DeveloperNode returns worker_id ----------


class TestDeveloperNodeWorkerId:
    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_success_includes_worker_id(self, mock_github_cls, mock_api, mock_spawn):
        """DeveloperNode should include worker_id from SpawnResult on success."""
        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="All done!",
            commit_sha="abc123",
            worker_id="dev-test-abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        result = await node.run(
            {
                "project_spec": _project(status=ProjectStatus.ACTIVE.value),
                "action": "create",
                "errors": [],
            }
        )

        assert result["engineering_status"] == "done"
        assert result["worker_id"] == "dev-test-abc123"


# ---------- 3.2 + 3.3: CI fix reuses worker ----------


class TestCIFixWorkerReuse:
    """Tests for _wait_for_ci_and_fix using existing worker."""

    @pytest.mark.asyncio
    @patch("src.consumers._ci_gate._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.send_task_to_worker", new_callable=AsyncMock)
    @patch("shared.clients.github.GitHubAppClient")
    async def test_reuses_worker_when_worker_id_available(
        self, mock_github_cls, mock_send_task, mock_publish, mock_record
    ):
        """When worker_id is set, CI fix should use send_task_to_worker instead of respawn."""
        mock_github = MagicMock()
        mock_github_cls.return_value = mock_github
        # First CI check: fails. Second: passes.
        mock_github.wait_for_workflow_completion = AsyncMock(
            side_effect=[
                RuntimeError(
                    "Workflow ci.yml failed: failure. "
                    "See: https://github.com/org/repo/actions/runs/111"
                ),
                {"id": 222, "conclusion": "success"},
            ]
        )
        mock_github.get_workflow_failure_logs = AsyncMock(return_value="test_foo.py FAILED")

        mock_send_task.return_value = SpawnResult(
            request_id="req-fix",
            success=True,
            exit_code=0,
            output="Fixed",
            worker_id="dev-test-abc",
        )

        from src.consumers._ci_gate import _wait_for_ci_and_fix

        redis = MagicMock()
        passed, attempts, *_ = await _wait_for_ci_and_fix(
            project=_project(),
            git_url="https://github.com/org/test-project",
            task_id="task-1",
            callback_stream="cb:1",
            redis=redis,
            worker_id="dev-test-abc",
        )

        assert passed is True
        mock_send_task.assert_called_once()
        # Verify worker_id was passed
        call_kwargs = mock_send_task.call_args
        assert call_kwargs.kwargs["worker_id"] == "dev-test-abc"

    @pytest.mark.asyncio
    @patch("src.consumers._ci_gate._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate._respawn_developer_for_ci_fix", new_callable=AsyncMock)
    @patch("shared.clients.github.GitHubAppClient")
    async def test_without_worker_id_uses_respawn(
        self, mock_github_cls, mock_respawn, mock_publish, mock_record
    ):
        """Without worker_id, should use old _respawn_developer_for_ci_fix path."""
        mock_github = MagicMock()
        mock_github_cls.return_value = mock_github

        mock_github.wait_for_workflow_completion = AsyncMock(
            side_effect=[
                RuntimeError(
                    "Workflow ci.yml failed: failure. "
                    "See: https://github.com/org/repo/actions/runs/111"
                ),
                {"id": 222, "conclusion": "success"},
            ]
        )
        mock_github.get_workflow_failure_logs = AsyncMock(return_value="test_foo.py FAILED")
        mock_respawn.return_value = (True, None)

        from src.consumers._ci_gate import _wait_for_ci_and_fix

        redis = MagicMock()
        passed, attempts, *_ = await _wait_for_ci_and_fix(
            project=_project(),
            git_url="https://github.com/org/test-project",
            task_id="task-1",
            callback_stream="cb:1",
            redis=redis,
            worker_id=None,
        )

        assert passed is True
        mock_respawn.assert_called_once()


# ---------- 3.4: Cleanup: delete worker after CI gate ----------


class TestCIFixCleanup:
    @pytest.mark.asyncio
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_delete_worker_called_after_ci_gate(
        self, mock_publish, mock_api, mock_ci_fix, mock_delete
    ):
        """delete_worker should be called after CI gate when worker_id is present."""
        mock_ci_fix.return_value = (True, [{"attempt": 0, "status": "passed"}], False, None)
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()

        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
            "worker_id": "dev-test-abc",
        }

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        await _handle_engineering_success(
            result=result_data,
            task_id="task-1",
            project=_project(),
            callback_stream="cb:1",
            redis=redis_mock,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="user-1",
        )

        mock_delete.assert_called_once_with("dev-test-abc", reason="completed")

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_no_delete_when_no_worker_id(
        self, mock_publish, mock_api, mock_ci_fix, mock_delete
    ):
        """delete_worker should NOT be called when no worker_id."""
        mock_ci_fix.return_value = (True, [{"attempt": 0, "status": "passed"}], False, None)
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()

        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
        }

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        await _handle_engineering_success(
            result=result_data,
            task_id="task-1",
            project=_project(),
            callback_stream="cb:1",
            redis=redis_mock,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="user-1",
        )

        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._wait_for_ci_and_fix", new_callable=AsyncMock)
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_worker_id_passed_to_ci_fix(
        self, mock_publish, mock_api, mock_ci_fix, mock_delete
    ):
        """_handle_engineering_success should pass worker_id to _wait_for_ci_and_fix."""
        mock_ci_fix.return_value = (True, [{"attempt": 0, "status": "passed"}], False, None)
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()

        from src.consumers.engineering import _handle_engineering_success

        result_data = {
            "engineering_status": "done",
            "commit_sha": "abc123",
            "worker_id": "dev-test-abc",
        }

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        await _handle_engineering_success(
            result=result_data,
            task_id="task-1",
            project=_project(),
            callback_stream="cb:1",
            redis=redis_mock,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="user-1",
        )

        mock_ci_fix.assert_called_once()
        call_kwargs = mock_ci_fix.call_args.kwargs
        assert call_kwargs["worker_id"] == "dev-test-abc"


# ---------- 3.5: Fallback on dead worker ----------


class TestCIFixFallback:
    @pytest.mark.asyncio
    @patch("src.consumers._ci_gate._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate._respawn_developer_for_ci_fix", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.send_task_to_worker", new_callable=AsyncMock)
    @patch("shared.clients.github.GitHubAppClient")
    async def test_fallback_on_send_task_timeout(
        self, mock_github_cls, mock_send_task, mock_respawn, mock_publish, mock_record
    ):
        """When send_task_to_worker times out, should fall back to _respawn."""
        mock_github = MagicMock()
        mock_github_cls.return_value = mock_github

        mock_github.wait_for_workflow_completion = AsyncMock(
            side_effect=[
                RuntimeError(
                    "Workflow ci.yml failed: failure. "
                    "See: https://github.com/org/repo/actions/runs/111"
                ),
                {"id": 222, "conclusion": "success"},
            ]
        )
        mock_github.get_workflow_failure_logs = AsyncMock(return_value="test_foo.py FAILED")

        # send_task_to_worker returns timeout (worker likely dead)
        mock_send_task.return_value = SpawnResult(
            request_id="req-fix",
            success=False,
            exit_code=-1,
            output="Timeout",
            error_message="execution_timeout",
            worker_id="dev-test-abc",
        )

        # Fallback to respawn succeeds
        mock_respawn.return_value = (True, None)

        from src.consumers._ci_gate import _wait_for_ci_and_fix

        redis = MagicMock()
        passed, attempts, *_ = await _wait_for_ci_and_fix(
            project=_project(),
            git_url="https://github.com/org/test-project",
            task_id="task-1",
            callback_stream="cb:1",
            redis=redis,
            worker_id="dev-test-abc",
        )

        assert passed is True
        mock_send_task.assert_called_once()
        mock_respawn.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.consumers._ci_gate._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.publish_callback_event", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate._respawn_developer_for_ci_fix", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.send_task_to_worker", new_callable=AsyncMock)
    @patch("shared.clients.github.GitHubAppClient")
    async def test_fallback_resets_worker_id_for_next_iteration(
        self, mock_github_cls, mock_send_task, mock_respawn, mock_publish, mock_record
    ):
        """After fallback, worker_id should be reset so next iteration also uses respawn."""
        mock_github = MagicMock()
        mock_github_cls.return_value = mock_github

        # CI fails twice: first attempt uses send_task (timeout), second uses respawn
        mock_github.wait_for_workflow_completion = AsyncMock(
            side_effect=[
                RuntimeError(
                    "Workflow ci.yml failed. See: https://github.com/org/repo/actions/runs/111"
                ),
                RuntimeError(
                    "Workflow ci.yml failed. See: https://github.com/org/repo/actions/runs/222"
                ),
                {"id": 333, "conclusion": "success"},
            ]
        )
        mock_github.get_workflow_failure_logs = AsyncMock(return_value="FAILED")

        # First attempt: send_task timeout → fallback to respawn
        mock_send_task.return_value = SpawnResult(
            request_id="req-fix",
            success=False,
            exit_code=-1,
            output="Timeout",
            error_message="execution_timeout",
            worker_id="dev-test-abc",
        )
        mock_respawn.return_value = (True, None)

        from src.consumers._ci_gate import _wait_for_ci_and_fix

        redis = MagicMock()
        passed, attempts, *_ = await _wait_for_ci_and_fix(
            project=_project(),
            git_url="https://github.com/org/test-project",
            task_id="task-1",
            callback_stream="cb:1",
            redis=redis,
            worker_id="dev-test-abc",
        )

        assert passed is True
        # send_task called only once (first attempt)
        mock_send_task.assert_called_once()
        # respawn called twice (fallback on first, then second attempt without worker_id)
        assert mock_respawn.call_count == 2  # noqa: PLR2004


# ---------- 4.3: Total gate timeout ----------


class TestTotalGateTimeout:
    @pytest.mark.asyncio
    @patch("src.config.constants.CI.TOTAL_GATE_TIMEOUT", 0)
    @patch("src.consumers._ci_gate._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.publish_callback_event", new_callable=AsyncMock)
    async def test_total_gate_timeout_aborts_loop(self, mock_publish, mock_record):
        """Total gate timeout should abort CI loop even if individual turns don't timeout."""
        from src.consumers._ci_gate import _wait_for_ci_and_fix

        redis = MagicMock()

        passed, attempts, *_ = await _wait_for_ci_and_fix(
            project=_project(),
            git_url="https://github.com/org/test-project",
            task_id="task-1",
            callback_stream="cb:1",
            redis=redis,
        )

        assert passed is False
        # Should have exactly one attempt recorded as "gate_timeout"
        assert len(attempts) == 1
        assert attempts[0]["status"] == "gate_timeout"
