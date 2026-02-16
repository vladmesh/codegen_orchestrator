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
