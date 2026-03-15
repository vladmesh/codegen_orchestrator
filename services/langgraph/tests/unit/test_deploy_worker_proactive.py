"""Unit tests for deploy worker proactive notifications.

When callback_stream is absent (webhook-triggered deploy), the worker
should send story events to po:input (routed through PO) instead of
sending raw messages directly to po:proactive.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import PO_INPUT_QUEUE, PO_PROACTIVE_QUEUE


@pytest.fixture
def mock_redis():
    """Mock RedisStreamClient."""
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired by default
    r.redis.delete = AsyncMock()
    r.redis.incr = AsyncMock(return_value=1)
    r.redis.expire = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    """Patch api_client methods used by the worker."""
    with patch("src.consumers.deploy.api_client") as api:
        api.patch = AsyncMock()
        api.get = AsyncMock(return_value=[])  # no existing running deploys (dedup)
        api.get_project = AsyncMock(
            return_value={
                "name": "my-project",
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/my-project"}
        )
        yield api


@pytest.fixture
def mock_allocations():
    """Patch allocation lookup (lazy import inside process_deploy_job)."""
    mock_fn = AsyncMock(return_value={"server_ip": "1.2.3.4", "port": 8080})
    with (
        patch("src.tools.allocator.ensure_project_allocations", mock_fn),
        patch("src.tools.allocator.AllocationError", Exception),
    ):
        yield mock_fn


@pytest.fixture
def mock_devops_subgraph():
    """Patch DevOps subgraph creation."""
    with patch("src.consumers.deploy.create_devops_subgraph") as factory:
        graph = AsyncMock()
        factory.return_value = graph
        yield graph


def _job(*, callback_stream=None, user_id="12345", triggered_by=DeployTrigger.WEBHOOK):
    return {
        "task_id": "deploy-wh-abc",
        "project_id": "proj-1",
        "user_id": user_id,
        "callback_stream": callback_stream or "",
        "triggered_by": triggered_by.value,
    }


@pytest.mark.asyncio
async def test_deploy_worker_sends_proactive_on_success(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={"deployed_url": "http://1.2.3.4:8080", "deployment_result": {}}
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "success"

    # Should have sent story_completed event to po:input (not po:proactive)
    story_calls = [c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_INPUT_QUEUE]
    assert len(story_calls) == 1
    msg = story_calls[0][0][1]
    assert msg["event"] == "story_completed"
    assert "my-project" in msg["text"]
    assert msg["user_id"] == "12345"

    # No direct proactive messages
    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 0


@pytest.mark.asyncio
async def test_deploy_worker_no_proactive_on_missing_secrets(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Deploy failures should NOT send proactive messages (spam filter)."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={"missing_user_secrets": ["STRIPE_KEY", "SENTRY_DSN"]}
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    assert "STRIPE_KEY" in result["error"]

    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 0


@pytest.mark.asyncio
async def test_deploy_worker_no_proactive_on_error(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Deploy failures should NOT send proactive messages (spam filter)."""
    mock_devops_subgraph.ainvoke = AsyncMock(return_value={"errors": ["Workflow timed out"]})

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"

    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 0


@pytest.mark.asyncio
async def test_deploy_worker_uses_callback_stream_when_present(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """When callback_stream is set (PO-initiated deploy), use it instead of proactive."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={"deployed_url": "http://1.2.3.4:8080", "deployment_result": {}}
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(callback_stream="po:response:abc"), mock_redis)

    assert result["status"] == "success"

    # Should have used callback_stream, NOT proactive
    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 0

    # Should have callback stream messages (via publish_flat)
    callback_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == "po:response:abc"
    ]
    assert len(callback_calls) >= 1


@pytest.mark.asyncio
async def test_deploy_worker_skips_when_lock_held(mock_redis, mock_api):
    """When Redis lock is held by another consumer, cancel this deploy."""
    mock_redis.redis.set = AsyncMock(return_value=False)  # lock NOT acquired

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "cancelled"

    # Should have cancelled the new task via API
    cancel_calls = [c for c in mock_api.patch.call_args_list if "cancelled" in str(c)]
    assert len(cancel_calls) == 1
