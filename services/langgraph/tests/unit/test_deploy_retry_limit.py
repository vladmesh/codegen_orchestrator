"""Tests for deploy failure outcome storage.

Deploy worker now stores deploy_outcome in run.result instead of managing
story transitions. Retry tracking is handled by the dispatcher.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployOutcome


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with patch("src.consumers.deploy_failure_handler.api_client") as api:
        api.patch = AsyncMock()
        yield api


@pytest.mark.asyncio
async def test_deploy_failure_stores_retry_outcome(mock_redis, mock_api):
    """Default deploy failure stores RETRY outcome in run.result."""
    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    result = await _handle_deploy_failure(
        task_id="deploy-001",
        project_id="proj-1",
        story_id="story-1",
        error_msg="SSH pre-check failed",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
    )

    assert result["status"] == "failed"
    patch_call = mock_api.patch.call_args
    run_result = patch_call[1]["json"]["result"]
    assert run_result["deploy_outcome"] == DeployOutcome.RETRY.value


@pytest.mark.asyncio
async def test_deploy_failure_stores_give_up_outcome(mock_redis, mock_api):
    """GIVE_UP outcome is stored in run.result."""
    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    await _handle_deploy_failure(
        task_id="deploy-003",
        project_id="proj-1",
        story_id="story-1",
        error_msg="port already allocated",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
        deploy_outcome=DeployOutcome.GIVE_UP,
    )

    patch_call = mock_api.patch.call_args
    run_result = patch_call[1]["json"]["result"]
    assert run_result["deploy_outcome"] == DeployOutcome.GIVE_UP.value


@pytest.mark.asyncio
async def test_deploy_failure_stores_deploy_fix_attempt(mock_redis, mock_api):
    """deploy_fix_attempt is stored in run.result for dispatcher routing."""
    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    await _handle_deploy_failure(
        task_id="deploy-004",
        project_id="proj-1",
        story_id="story-1",
        error_msg="test error",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
        deploy_fix_attempt=2,
    )

    patch_call = mock_api.patch.call_args
    run_result = patch_call[1]["json"]["result"]
    assert run_result["deploy_fix_attempt"] == 2


@pytest.mark.asyncio
async def test_deploy_failure_does_not_transition_story(mock_redis, mock_api):
    """Deploy worker must NOT call transition_story — dispatcher handles this."""
    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    await _handle_deploy_failure(
        task_id="deploy-005",
        project_id="proj-1",
        story_id="story-1",
        error_msg="test error",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
    )

    # No Redis counter ops (dispatcher handles retry tracking now)
    mock_redis.redis.incr.assert_not_awaited()
    # No story transitions
    for call in mock_api.method_calls:
        assert "transition_story" not in str(call)
