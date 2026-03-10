"""Unit tests for CI gate reject flow and CI-fix template.

Tests:
- CI-fix prompt includes reject instructions
- CI gate handles worker rejected status
- CI gate propagates reject info to caller
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
    r.publish_flat = AsyncMock()
    return r


def _project():
    return {"id": "proj-1", "name": "test-project", "config": {"modules": ["backend"]}}


class TestCIFixPromptTemplate:
    """Tests for _build_ci_fix_prompt with reject instructions."""

    def test_prompt_includes_reject_instructions(self):
        from src.consumers._ci_gate import _build_ci_fix_prompt

        prompt = _build_ci_fix_prompt(
            failure_context="Job 'lint-and-test' failed:\n  Step 'Run tests' failed",
            attempt=1,
        )
        assert "## REJECTED" in prompt
        assert "infrastructure" in prompt.lower() or "not a code issue" in prompt.lower()

    def test_prompt_includes_failure_context(self):
        from src.consumers._ci_gate import _build_ci_fix_prompt

        ctx = "Job 'build-and-push (backend)' failed:\n  Step 'Run ruff check' failed"
        prompt = _build_ci_fix_prompt(failure_context=ctx, attempt=2)
        assert "build-and-push" in prompt
        assert "ruff check" in prompt

    def test_prompt_includes_run_url_when_provided(self):
        from src.consumers._ci_gate import _build_ci_fix_prompt

        prompt = _build_ci_fix_prompt(
            failure_context="test failure",
            attempt=1,
            run_url="https://github.com/org/repo/actions/runs/12345",
        )
        assert "https://github.com/org/repo/actions/runs/12345" in prompt

    def test_prompt_handles_empty_context(self):
        from src.consumers._ci_gate import _build_ci_fix_prompt

        prompt = _build_ci_fix_prompt(failure_context="", attempt=1)
        assert "## REJECTED" in prompt  # Must still include reject instructions


class TestCIGateRejectFlow:
    """Tests for CI gate handling of worker rejected status."""

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch("src.consumers._ci_gate._attempt_developer_fix", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.publish_callback_event", new_callable=AsyncMock)
    async def test_worker_reject_stops_retry_loop(
        self, mock_publish, mock_record, mock_fix, mock_gh_cls, mock_redis
    ):
        """When worker rejects, CI gate should stop immediately (no more retries)."""
        from src.consumers._ci_gate import _wait_for_ci_and_fix

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh

        # CI fails with code-looking failure
        mock_gh.wait_for_workflow_completion = AsyncMock(
            side_effect=RuntimeError(
                "Workflow ci.yml failed: failure. "
                "See: https://github.com/org/repo/actions/runs/12345"
            )
        )
        mock_gh.get_workflow_failure_logs = AsyncMock(
            return_value="Job 'build-and-push' failed:\n  Step 'Build Docker image' failed"
        )

        # Developer fix returns rejected
        mock_fix.return_value = (False, "dev-123", "REGISTRY_PASSWORD secret is empty")

        passed, ci_attempts, rejected, reject_reason = await _wait_for_ci_and_fix(
            project=_project(),
            git_url="https://github.com/org/repo",
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            developer_started_at=datetime(2025, 1, 1, tzinfo=UTC),
            user_id="u1",
        )

        assert passed is False
        assert rejected is True
        assert reject_reason == "REGISTRY_PASSWORD secret is empty"
        # Should only call fix once, not retry
        assert mock_fix.await_count == 1

    @pytest.mark.asyncio
    @patch("shared.clients.github.GitHubAppClient")
    @patch("src.consumers._ci_gate._attempt_developer_fix", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate._record_ci_attempts", new_callable=AsyncMock)
    @patch("src.consumers._ci_gate.publish_callback_event", new_callable=AsyncMock)
    async def test_normal_failure_still_retries(
        self, mock_publish, mock_record, mock_fix, mock_gh_cls, mock_redis
    ):
        """Non-rejected failures should still retry as before."""
        from src.consumers._ci_gate import _wait_for_ci_and_fix

        mock_gh = AsyncMock()
        mock_gh_cls.return_value = mock_gh

        # CI always fails
        mock_gh.wait_for_workflow_completion = AsyncMock(
            side_effect=RuntimeError(
                "Workflow ci.yml failed: failure. "
                "See: https://github.com/org/repo/actions/runs/12345"
            )
        )
        mock_gh.get_workflow_failure_logs = AsyncMock(
            return_value="Job 'lint-and-test' failed:\n  Step 'Run tests' failed"
        )

        # Developer fix succeeds but CI still fails (no reject)
        mock_fix.return_value = (True, "dev-123", None)

        passed, ci_attempts, rejected, reject_reason = await _wait_for_ci_and_fix(
            project=_project(),
            git_url="https://github.com/org/repo",
            task_id="eng-1",
            callback_stream="po:response:abc",
            redis=mock_redis,
            developer_started_at=datetime(2025, 1, 1, tzinfo=UTC),
            user_id="u1",
        )

        assert passed is False
        assert rejected is False
        assert reject_reason is None
