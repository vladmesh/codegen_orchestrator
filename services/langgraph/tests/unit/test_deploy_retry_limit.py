"""Tests for deploy failure retry limit.

When a deploy fails, the story rolls back to in_progress. But after MAX_DEPLOY_RETRIES
consecutive failures, the story should transition to failed instead of looping forever.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.redis.incr = AsyncMock()
    r.redis.get = AsyncMock(return_value=None)
    r.redis.expire = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with patch("src.consumers.deploy_failure_handler.api_client") as api:
        api.patch = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.transition_story = AsyncMock()
        api.get_project = AsyncMock(
            return_value={
                "id": "proj-1",
                "name": "my-project",
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/my-project"}
        )
        yield api


def _job(*, story_id="story-1"):
    return {
        "task_id": "deploy-test-001",
        "project_id": "proj-1",
        "user_id": "12345",
        "callback_stream": "",
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "story_id": story_id,
    }


@pytest.mark.asyncio
async def test_deploy_failure_rolls_back_when_under_limit(mock_redis, mock_api):
    """First failure should roll back story to in_progress (via 'start' action)."""
    mock_redis.redis.incr = AsyncMock(return_value=1)

    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    await _handle_deploy_failure(
        task_id="deploy-001",
        project_id="proj-1",
        story_id="story-1",
        error_msg="SSH pre-check failed",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
    )

    # Should roll back to in_progress
    mock_api.transition_story.assert_awaited_with("story-1", "start")


@pytest.mark.asyncio
async def test_deploy_failure_fails_story_when_limit_exceeded(mock_redis, mock_api):
    """After MAX_DEPLOY_RETRIES failures, story should transition to failed."""
    from src.consumers.deploy_failure_handler import MAX_DEPLOY_RETRIES

    mock_redis.redis.incr = AsyncMock(return_value=MAX_DEPLOY_RETRIES)

    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    await _handle_deploy_failure(
        task_id="deploy-003",
        project_id="proj-1",
        story_id="story-1",
        error_msg="SSH pre-check failed again",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
    )

    # Should transition to failed, not start
    mock_api.transition_story.assert_awaited_with("story-1", "fail")


@pytest.mark.asyncio
async def test_deploy_failure_increments_redis_counter(mock_redis, mock_api):
    """Each failure should increment the Redis counter."""
    mock_redis.redis.incr = AsyncMock(return_value=1)

    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    await _handle_deploy_failure(
        task_id="deploy-004",
        project_id="proj-1",
        story_id="story-1",
        error_msg="test error",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
    )

    mock_redis.redis.incr.assert_awaited_once_with("deploy:story-1:attempts")
    mock_redis.redis.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_deploy_failure_skips_counter_without_story_id(mock_redis, mock_api):
    """Without story_id, should not use counter and always roll back."""
    from src.consumers.deploy_failure_handler import _handle_deploy_failure

    await _handle_deploy_failure(
        task_id="deploy-005",
        project_id="proj-1",
        story_id="",
        error_msg="test error",
        callback_stream="",
        user_id="12345",
        redis=mock_redis,
    )

    mock_redis.redis.incr.assert_not_awaited()
