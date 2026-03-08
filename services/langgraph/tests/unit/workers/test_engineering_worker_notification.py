"""Integration-style unit tests for Level 1+2 cascade failure fixes.

Tests the full _handle_engineering_success flow with real callback event
publishing (using mocked Redis xadd), verifying that:
- commit_sha=None blocks the pipeline (Level 1)
- Deploying sends "progress" not "completed" (Level 2)
- Deploy trigger failure notifies user (Level 2)

These tests go beyond simple mocks by verifying the actual event
payloads published to callback streams.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient with xadd/publish_flat call recording."""
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
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/test"}
        )
        yield api


def _project():
    return {"id": "proj-1", "name": "test-project", "config": {"modules": ["backend"]}}


def _get_callback_events(mock_redis, stream="po:response:abc"):
    """Extract callback events from publish_flat calls, returning list of field dicts."""
    events = []
    for call in mock_redis.publish_flat.call_args_list:
        args = call[0]
        if args[0] == stream:
            events.append(args[1])
    return events


class TestLevel1CascadeFailure:
    """Verify commit_sha=None stops the entire pipeline and notifies user."""

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_no_commit_sha_full_event_chain(self, mock_ci_gate, mock_redis, mock_api):
        """commit_sha=None: task=failed, callback=failed, no CI check, no deploy."""
        mock_ci_gate.return_value = (True, [])  # Should never be reached

        from src.workers.engineering_worker import _handle_engineering_success

        out = await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": None},
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        # Pipeline stopped
        assert out["status"] == "failed"

        # CI gate was never called
        mock_ci_gate.assert_not_awaited()

        # User got a "failed" event with meaningful message
        events = _get_callback_events(mock_redis)
        assert len(events) == 1
        assert events[0]["event"] == "failed"
        assert "commit" in events[0]["text"].lower()

        # No deploy queue message
        deploy_calls = [c for c in mock_redis.redis.xadd.call_args_list if "deploy" in str(c[0][0])]
        assert len(deploy_calls) == 0

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_empty_string_commit_sha_also_blocks(self, mock_ci_gate, mock_redis, mock_api):
        """Empty string commit_sha is also falsy — should block."""
        mock_ci_gate.return_value = (True, [])

        from src.workers.engineering_worker import _handle_engineering_success

        out = await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": ""},
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        assert out["status"] == "failed"
        mock_ci_gate.assert_not_awaited()


class TestLevel2NotificationDecoupling:
    """Verify notification type matches actual pipeline state."""

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_deploy_path_sends_progress_then_no_completed(
        self, mock_ci_gate, mock_redis, mock_api
    ):
        """skip_deploy=False: user gets 'progress' not 'completed' from engineering worker."""
        mock_ci_gate.return_value = (True, [])

        from src.workers.engineering_worker import _handle_engineering_success

        out = await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": "abc123"},
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        assert out["status"] == "success"

        events = _get_callback_events(mock_redis)
        event_types = [e["event"] for e in events]

        # Must have "progress" with deploy message
        assert "progress" in event_types
        progress_texts = [e["text"] for e in events if e["event"] == "progress"]
        assert any("deploy" in t.lower() for t in progress_texts)

        # Must NOT have "completed" — deploy worker will send that
        assert "completed" not in event_types

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_skip_deploy_sends_completed(self, mock_ci_gate, mock_redis, mock_api):
        """skip_deploy=True: user gets 'completed' — this IS the final step."""
        mock_ci_gate.return_value = (True, [])

        from src.workers.engineering_worker import _handle_engineering_success

        out = await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": "abc123"},
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=True,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        assert out["status"] == "success"

        events = _get_callback_events(mock_redis)
        event_types = [e["event"] for e in events]
        assert "completed" in event_types

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_deploy_queue_failure_notifies_user(self, mock_ci_gate, mock_redis, mock_api):
        """When deploy trigger fails, user gets 'failed' event — not silence."""
        mock_ci_gate.return_value = (True, [])
        mock_api.post.side_effect = RuntimeError("API unreachable")

        from src.workers.engineering_worker import _handle_engineering_success

        await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": "abc123"},
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        events = _get_callback_events(mock_redis)
        event_types = [e["event"] for e in events]

        # Must have a "failed" event about deploy trigger
        assert "failed" in event_types
        failed_texts = [e["text"] for e in events if e["event"] == "failed"]
        assert any("deploy" in t.lower() for t in failed_texts)

    @pytest.mark.asyncio
    @patch("src.workers.engineering_worker._wait_for_ci_and_fix", new_callable=AsyncMock)
    async def test_ci_failure_sends_failed_not_completed(self, mock_ci_gate, mock_redis, mock_api):
        """CI failure: user gets 'failed', never 'completed'."""
        mock_ci_gate.return_value = (
            False,
            [{"attempt": 0, "status": "failed", "failure_context": ""}],
        )

        from src.workers.engineering_worker import _handle_engineering_success

        out = await _handle_engineering_success(
            result={"engineering_status": "done", "commit_sha": "abc123"},
            task_id="eng-1",
            project=_project(),
            callback_stream="po:response:abc",
            redis=mock_redis,
            skip_deploy=False,
            developer_started_at=datetime.now(UTC),
            user_id="u1",
        )

        assert out["status"] == "failed"

        events = _get_callback_events(mock_redis)
        event_types = [e["event"] for e in events]
        assert "failed" in event_types
        assert "completed" not in event_types
