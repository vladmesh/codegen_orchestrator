"""Unit tests for deploy worker smoke result handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import PO_PROACTIVE_QUEUE


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.publish_flat = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with patch("src.consumers.deploy.api_client") as api:
        api.patch = AsyncMock()
        api.get = AsyncMock(return_value=[])
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
        project={"name": "test"},
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
    """When smoke fails, task is marked failed but project stays active."""
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

    # Project status should be active (deploy succeeded)
    project_status_calls = [c for c in mock_api.patch.call_args_list if "projects/" in str(c)]
    # Last project status update should be active (not failed)
    last_project_update = project_status_calls[-1]
    assert last_project_update[1]["json"]["status"] == "active"

    # Should send proactive notification about smoke failure
    proactive_calls = [
        c for c in mock_redis.publish_flat.call_args_list if c[0][0] == PO_PROACTIVE_QUEUE
    ]
    assert len(proactive_calls) == 1
    assert "smoke" in proactive_calls[0][0][1]["text"].lower()


@pytest.mark.asyncio
async def test_deploy_worker_missing_secrets_resets_project_status(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """When missing_user_secrets, project status must be rolled back from deploying."""
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

    # Project status must be rolled back — NOT left as "deploying"
    project_patch_calls = [c for c in mock_api.patch.call_args_list if "projects/proj-1" in str(c)]
    # Should have: set to deploying, then rollback
    assert len(project_patch_calls) >= 2  # noqa: PLR2004
    last_project_status = project_patch_calls[-1][1]["json"]["status"]
    assert (
        last_project_status != "deploying"
    ), "Project stuck in deploying — missing_user_secrets must roll back status"
