"""Unit tests for deploy worker proactive notifications.

When callback_stream is absent (webhook-triggered deploy), the worker
should send notifications to po:proactive instead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import PO_PROACTIVE_QUEUE


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
    """Patch api_client methods used by the worker."""
    with patch("src.workers.deploy_worker.api_client") as api:
        api.patch = AsyncMock()
        api.get = AsyncMock(return_value=[])  # no existing running deploys (dedup)
        api.get_project = AsyncMock(
            return_value={
                "name": "my-project",
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"git_url": "https://github.com/org/my-project"}
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
    with patch("src.workers.deploy_worker.create_devops_subgraph") as factory:
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

    from src.workers.deploy_worker import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "success"

    # Should have sent proactive message via publish_flat (no callback_stream)
    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 1
    msg = proactive_calls[0][0][1]
    assert "Deployed my-project" in msg["text"]
    assert msg["user_id"] == "12345"


@pytest.mark.asyncio
async def test_deploy_worker_sends_proactive_on_missing_secrets(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={"missing_user_secrets": ["STRIPE_KEY", "SENTRY_DSN"]}
    )

    from src.workers.deploy_worker import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    assert "STRIPE_KEY" in result["error"]

    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 1
    msg = proactive_calls[0][0][1]
    assert "Deploy blocked" in msg["text"]
    assert "STRIPE_KEY" in msg["text"]


@pytest.mark.asyncio
async def test_deploy_worker_sends_proactive_on_error(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    mock_devops_subgraph.ainvoke = AsyncMock(return_value={"errors": ["Workflow timed out"]})

    from src.workers.deploy_worker import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"

    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 1
    msg = proactive_calls[0][0][1]
    assert "Deploy failed" in msg["text"]


@pytest.mark.asyncio
async def test_deploy_worker_uses_callback_stream_when_present(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """When callback_stream is set (PO-initiated deploy), use it instead of proactive."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={"deployed_url": "http://1.2.3.4:8080", "deployment_result": {}}
    )

    from src.workers.deploy_worker import process_deploy_job

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
async def test_deploy_worker_skips_when_already_running(mock_redis, mock_api):
    """When another deploy is already running for the same project, cancel this one."""
    # Mock API: running query returns existing task, queued query not reached
    mock_api.get = AsyncMock(return_value=[{"id": "deploy-existing-123"}])

    from src.workers.deploy_worker import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "cancelled"
    assert result["existing_task_id"] == "deploy-existing-123"

    # Should have cancelled the new task via API
    mock_api.patch.assert_called_once()
    patch_args = mock_api.patch.call_args
    assert "cancelled" in str(patch_args)


@pytest.mark.asyncio
async def test_deploy_worker_skips_when_another_queued(mock_redis, mock_api):
    """When another deploy is queued for the same project, cancel this one (BUG 13)."""
    # Mock API: running query returns nothing, queued query returns existing task
    mock_api.get = AsyncMock(
        side_effect=[
            [],  # no running tasks
            [{"id": "deploy-queued-456"}],  # queued task found
        ]
    )

    from src.workers.deploy_worker import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "cancelled"
    assert result["existing_task_id"] == "deploy-queued-456"


@pytest.mark.asyncio
async def test_deploy_worker_dedup_ignores_self(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Dedup guard should not cancel itself if its own task_id appears in queued results."""
    # Mock API: running=empty, queued returns only self
    mock_api.get = AsyncMock(
        side_effect=[
            [],  # no running tasks
            [{"id": "deploy-wh-abc"}],  # self is queued (same task_id as _job())
        ]
    )
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={"deployed_url": "http://1.2.3.4:8080", "deployment_result": {}}
    )

    from src.workers.deploy_worker import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    # Should NOT be cancelled — the only queued task is itself
    assert result["status"] == "success"
