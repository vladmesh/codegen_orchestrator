"""Unit tests for deploy worker smoke result handling."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import PO_PROACTIVE_QUEUE
from tests.unit.factories import make_project, make_repository


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.redis.incr = AsyncMock(return_value=1)
    r.redis.expire = AsyncMock()
    r.redis.exists = AsyncMock(return_value=False)  # no live teardown fence
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
        patch("src.allocations.ensure_project_allocations", mock_fn),
        patch("src.allocations.AllocationError", Exception),
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
        "head_sha": "a" * 40,
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
async def test_unproven_cancellation_propagates_not_masked_as_failure(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """An unproven Actions cancellation must reach the live-work fence, not become a failed run."""
    from shared.clients.github import WorkflowCancellationUnprovenError

    mock_devops_subgraph.ainvoke = AsyncMock(
        side_effect=WorkflowCancellationUnprovenError("could not verify stop")
    )

    from src.consumers.deploy import process_deploy_job

    with pytest.raises(WorkflowCancellationUnprovenError):
        await process_deploy_job(_job(), mock_redis)

    # Never patched the run into a terminal failed/give-up state.
    assert not [c for c in mock_api.patch.call_args_list if "failed" in str(c)]
    # Deploy lock released via finally so the next attempt can proceed.
    mock_redis.redis.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_deploy_error_under_teardown_fences_cleanup_without_ack(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """A transient failure while teardown fences the project must not settle as a deploy failure.

    The dispatched deploy.yml run can still be executing, so the entry stays unacked
    and cleanup sees `live:work:failed` before it deletes external or DB resources.
    """
    from src.consumers._live_work import execute_live_work, live_work_failure_key
    from src.consumers.deploy import process_deploy_job

    # The cancel check inside wait_for_workflow_completion lost the API once.
    mock_devops_subgraph.ainvoke = AsyncMock(side_effect=ConnectionError("runs API unreachable"))
    mock_redis.redis.exists = AsyncMock(return_value=True)  # live:work:cancelled is set
    mock_redis.redis.eval = AsyncMock(return_value=1)  # lease granted
    mock_redis.redis.zrem = AsyncMock()
    mock_redis.ack = AsyncMock()

    with patch("src.consumers._live_work.LIVE_WORK_LEASE_REFRESH_SECONDS", 0):
        with pytest.raises(ConnectionError):
            await execute_live_work(
                mock_redis,
                queue="jobs:deploy",
                group="capability-workers",
                message_id="1-0",
                project_id="proj-1",
                process=lambda: process_deploy_job(_job(), mock_redis),
            )

    mock_redis.ack.assert_not_awaited()
    failure_writes = [
        c
        for c in mock_redis.redis.set.await_args_list
        if c.args[:1] == (live_work_failure_key("proj-1"),)
    ]
    assert failure_writes, "cleanup fence marker must be written"
    assert "cancel_settlement_failed" in failure_writes[0].args
    # The run was never patched into a terminal failed state.
    assert not [c for c in mock_api.patch.call_args_list if "failed" in str(c)]


@pytest.mark.asyncio
async def test_result_shaped_deploy_error_under_teardown_fences_cleanup_without_ack(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """A deployer error dict under teardown must not ACK the stream entry."""
    from src.consumers._live_work import execute_live_work, live_work_failure_key
    from src.consumers.deploy import process_deploy_job

    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployment_result": {"status": "failed", "error": "timed out"},
            "errors": ["Deploy timeout: timed out"],
        }
    )
    mock_redis.redis.exists = AsyncMock(return_value=True)  # live:work:cancelled is set
    mock_redis.redis.eval = AsyncMock(return_value=1)  # lease granted
    mock_redis.redis.zrem = AsyncMock()
    mock_redis.ack = AsyncMock()

    with patch("src.consumers._live_work.LIVE_WORK_LEASE_REFRESH_SECONDS", 0):
        with pytest.raises(RuntimeError, match="live work returned unsettled"):
            await execute_live_work(
                mock_redis,
                queue="jobs:deploy",
                group="capability-workers",
                message_id="1-0",
                project_id="proj-1",
                process=lambda: process_deploy_job(_job(), mock_redis),
            )

    mock_redis.ack.assert_not_awaited()
    failure_writes = [
        c
        for c in mock_redis.redis.set.await_args_list
        if c.args[:1] == (live_work_failure_key("proj-1"),)
    ]
    assert failure_writes, "cleanup fence marker must be written"
    assert "cancel_settlement_failed" in failure_writes[0].args


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
        head_sha="a" * 40,
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
            "missing_user_secrets": [
                {"key": "TELEGRAM_BOT_TOKEN", "description": "Telegram bot token"},
                {"key": "OPENAI_API_KEY", "description": "OpenAI API key"},
            ],
            "errors": [],
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    assert "missing" in result["error"].lower()
