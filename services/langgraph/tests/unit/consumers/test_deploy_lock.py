"""Unit tests for deploy Redis lock deduplication."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.factories import make_project, make_repository

from shared.contracts.queues.deploy import DeployTrigger


@pytest.fixture
def mock_redis():
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
    with (
        patch("src.consumers.deploy.api_client") as api,
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
    ):
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.get_project = AsyncMock(
            return_value=make_project(
                name="my-project",
                config={"modules": ["backend"]},
            )
        )
        api.get_primary_repository = AsyncMock(
            return_value=make_repository(git_url="https://github.com/org/my-project")
        )
        yield api


@pytest.fixture
def mock_allocations():
    mock_fn = AsyncMock(return_value={"server_ip": "1.2.3.4", "port": 8080})
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


def _job(*, task_id="deploy-lock-1", project_id="proj-1"):
    return {
        "task_id": task_id,
        "project_id": project_id,
        "user_id": "12345",
        "callback_stream": "",
        "triggered_by": DeployTrigger.WEBHOOK.value,
    }


@pytest.mark.asyncio
async def test_lock_acquired_before_processing(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Deploy acquires Redis lock before running subgraph."""
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
    # Lock acquired with SET NX
    mock_redis.redis.set.assert_called_once()
    call_kwargs = mock_redis.redis.set.call_args
    assert call_kwargs[1].get("nx") is True
    assert "deploy:proj-1:lock" in call_kwargs[0]


@pytest.mark.asyncio
async def test_lock_not_acquired_cancels_deploy(mock_redis, mock_api):
    """When lock is held by another consumer, deploy is cancelled."""
    mock_redis.redis.set = AsyncMock(return_value=False)  # lock NOT acquired

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "cancelled"
    # Run marked cancelled in DB
    cancel_calls = [
        c for c in mock_api.patch.call_args_list if "runs/" in str(c) and "cancelled" in str(c)
    ]
    assert len(cancel_calls) == 1


@pytest.mark.asyncio
async def test_lock_released_on_success(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Lock is released after successful deploy."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {"status": "pass", "checks": []},
        }
    )

    from src.consumers.deploy import process_deploy_job

    await process_deploy_job(_job(), mock_redis)

    mock_redis.redis.delete.assert_called_once_with("deploy:proj-1:lock")


@pytest.mark.asyncio
async def test_lock_released_on_failure(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Lock is released even when deploy fails."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": None,
            "errors": ["Workflow failed"],
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    mock_redis.redis.delete.assert_called_once_with("deploy:proj-1:lock")


@pytest.mark.asyncio
async def test_lock_released_on_exception(mock_redis, mock_api, mock_allocations):
    """Lock is released even on unhandled exception."""
    with patch("src.consumers.deploy.create_devops_subgraph") as factory:
        factory.side_effect = RuntimeError("boom")

        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    mock_redis.redis.delete.assert_called_once_with("deploy:proj-1:lock")
