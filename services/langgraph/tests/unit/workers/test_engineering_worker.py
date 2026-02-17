"""Unit tests for engineering worker fail-fast checks.

Tests commit_sha gate in _handle_engineering_success and
CI gate fail-closed behavior in _wait_for_ci_and_fix.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient."""
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
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
        mock_ci_gate.return_value = True  # Should never be reached

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

        # Callback must be "failed"
        xadd_calls = mock_redis.redis.xadd.call_args_list
        failed_events = [c for c in xadd_calls if c[0][1].get("event") == "failed"]
        assert len(failed_events) >= 1

        # Deploy queue must NOT have been written to
        deploy_calls = [c for c in xadd_calls if "deploy" in str(c[0][0])]
        assert len(deploy_calls) == 0

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_with_commit_sha_proceeds(self, mock_ci_gate, mock_redis, mock_api):
        """commit_sha present must proceed to CI gate and then deploy."""
        mock_ci_gate.return_value = True

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


class TestNotificationDecoupling:
    """Tests that notification type is decoupled from deploy trigger."""

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_ci_passed_sends_progress_when_deploying(
        self, mock_ci_gate, mock_redis, mock_api
    ):
        """skip_deploy=False → event type is 'progress', not 'completed'."""
        mock_ci_gate.return_value = True

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

        # Find callback events on the callback stream
        xadd_calls = mock_redis.redis.xadd.call_args_list
        callback_events = [c for c in xadd_calls if c[0][0] == "po:response:abc"]

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
        mock_ci_gate.return_value = True

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

        # Find callback events on the callback stream
        xadd_calls = mock_redis.redis.xadd.call_args_list
        callback_events = [c for c in xadd_calls if c[0][0] == "po:response:abc"]

        # There should be a "completed" event
        completed_events = [c for c in callback_events if c[0][1].get("event") == "completed"]
        assert len(completed_events) == 1

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_deploy_trigger_failure_publishes_failed_event(
        self, mock_ci_gate, mock_redis, mock_api
    ):
        """When deploy queuing fails, user gets a 'failed' notification."""
        mock_ci_gate.return_value = True
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

        # Find callback events on the callback stream
        xadd_calls = mock_redis.redis.xadd.call_args_list
        callback_events = [c for c in xadd_calls if c[0][0] == "po:response:abc"]

        # There should be a "failed" event about deploy trigger
        failed_events = [c for c in callback_events if c[0][1].get("event") == "failed"]
        assert len(failed_events) >= 1


class TestCIGateFailClosed:
    @pytest.mark.asyncio
    async def test_missing_repo_url_returns_false(self, mock_redis):
        """CI gate must fail-closed (return False) when project has no repository_url."""
        from src.workers.engineering_worker import _wait_for_ci_and_fix

        result = await _wait_for_ci_and_fix(
            project={"id": "p1"},
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            user_id="u1",
        )

        assert result is False
