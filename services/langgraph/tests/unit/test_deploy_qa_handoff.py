"""Unit tests for deploy → QA handoff.

After successful deploy+smoke, deploy consumer should transition story
to TESTING and publish QAMessage to qa:queue instead of completing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import QA_QUEUE


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.redis.incr = AsyncMock(return_value=1)
    r.redis.expire = AsyncMock()
    r.publish_flat = AsyncMock()
    r.publish_message = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with (
        patch("src.consumers.deploy.api_client") as api,
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
    ):
        api.patch = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.get_project = AsyncMock(
            return_value={
                "name": "weather-bot",
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={
                "id": "repo-1",
                "git_url": "https://github.com/org/weather-bot",
            }
        )
        api.transition_story = AsyncMock(return_value={})
        yield api


@pytest.fixture
def mock_allocations():
    mock_fn = AsyncMock(return_value={"srv:8000": {"server_ip": "1.2.3.4", "port": 8000}})
    with (
        patch("src.tools.allocator.ensure_project_allocations", mock_fn),
        patch("src.tools.allocator.AllocationError", Exception),
    ):
        yield mock_fn


@pytest.fixture
def mock_devops_subgraph():
    with patch("src.consumers.deploy.create_devops_subgraph") as factory:
        graph = AsyncMock()
        factory.return_value = graph
        yield graph


def _job(*, story_id="story-1", user_id="12345"):
    return {
        "task_id": "deploy-qa-1",
        "project_id": "proj-1",
        "user_id": user_id,
        "story_id": story_id,
        "callback_stream": "",
        "triggered_by": DeployTrigger.ENGINEERING.value,
    }


@pytest.mark.asyncio
async def test_deploy_success_transitions_to_testing(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Successful deploy should transition story to TESTING, not COMPLETED."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {"status": "pass", "checks": []},
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "success"
    # Story should transition to "test", NOT "complete"
    mock_api.transition_story.assert_called_once_with("story-1", "test")


@pytest.mark.asyncio
async def test_deploy_success_publishes_qa_message(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Successful deploy should publish QAMessage to qa:queue."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {"status": "pass", "checks": []},
        }
    )

    from src.consumers.deploy import process_deploy_job

    await process_deploy_job(_job(), mock_redis)

    # Should publish QAMessage to qa:queue
    mock_redis.publish_message.assert_called_once()
    call_args = mock_redis.publish_message.call_args
    assert call_args[0][0] == QA_QUEUE
    qa_msg = call_args[0][1]
    assert qa_msg.story_id == "story-1"
    assert qa_msg.project_id == "proj-1"
    assert qa_msg.deployed_url == "http://1.2.3.4:8080"
    assert qa_msg.user_id == "12345"


@pytest.mark.asyncio
async def test_deploy_success_does_not_delete_worker(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Worker container should NOT be deleted — QA may need it for fixes."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {"status": "pass", "checks": []},
        }
    )

    with patch("src.consumers.deploy_failure_handler.delete_worker") as mock_delete:
        from src.consumers.deploy import process_deploy_job

        await process_deploy_job(_job(), mock_redis)

        mock_delete.assert_not_called()


@pytest.mark.asyncio
async def test_deploy_success_no_story_skips_qa(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Deploy without story_id should complete normally (no QA)."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {"status": "pass", "checks": []},
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(story_id=""), mock_redis)

    assert result["status"] == "success"
    # No QA message published when no story
    mock_redis.publish_message.assert_not_called()
