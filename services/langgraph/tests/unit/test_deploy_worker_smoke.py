"""Unit tests for deploy worker smoke result handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.factories import make_project, make_repository

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import PO_PROACTIVE_QUEUE


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
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


def _job(*, callback_stream=None, user_id="12345"):
    return {
        "task_id": "deploy-smoke-1",
        "project_id": "proj-1",
        "user_id": user_id,
        "callback_stream": callback_stream or "",
        "triggered_by": DeployTrigger.WEBHOOK.value,
    }


@pytest.mark.asyncio
async def test_deploy_worker_smoke_pass(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """When smoke passes, task result includes smoke_result."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {
                "status": "pass",
                "checks": [{"module": "backend", "result": "pass", "detail": "HTTP 200"}],
            },
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "success"
    # Task should be completed with smoke_result in result
    patch_calls = mock_api.patch.call_args_list
    run_complete_call = [c for c in patch_calls if "runs/" in str(c) and "completed" in str(c)]
    assert len(run_complete_call) == 1
    run_result = run_complete_call[0][1]["json"]["result"]
    assert run_result["smoke_result"]["status"] == "pass"


@pytest.mark.asyncio
async def test_build_subgraph_input_includes_smoke_result():
    """_build_subgraph_input must include smoke_result key so LangGraph tracks it."""
    from src.consumers.deploy import _build_subgraph_input

    result = _build_subgraph_input(
        project_id="proj-1",
        project=make_project(name="test"),
        git_url="https://github.com/org/repo",
        allocated_resources={"srv:8000": {"server_ip": "1.2.3.4", "port": 8000}},
        job_data={},
    )
    assert "smoke_result" in result, "smoke_result must be initialized in subgraph input"
    assert result["smoke_result"] is None


@pytest.mark.asyncio
async def test_deploy_worker_smoke_fail(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """When smoke fails, task is marked failed."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {
                "status": "fail",
                "checks": [
                    {"module": "backend", "result": "fail", "detail": "HTTP 500"},
                ],
            },
            "errors": ["Smoke failed: backend health check — HTTP 500"],
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    assert "smoke" in result["error"].lower()

    # No project service_status updates (Application status is the source of truth)
    project_patch_calls = [c for c in mock_api.patch.call_args_list if "projects/" in str(c)]
    assert len(project_patch_calls) == 0

    # Smoke failure is internal — no proactive message (spam filter)
    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 0


@pytest.mark.asyncio
async def test_deploy_worker_missing_secrets_fails(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """When missing_user_secrets, deploy should fail."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": None,
            "missing_user_secrets": ["TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY"],
            "errors": [],
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    assert "missing" in result["error"].lower()
