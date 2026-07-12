"""Unit tests for deploy failure LLM classifier (CODE_FIX / RETRY / GIVE_UP).

Classification tests verify the LLM classifier returns the correct category.
Integration tests verify that deploy worker stores the classified outcome in run.result.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.factories import make_project, make_repository

from shared.contracts.queues.deploy import DeployOutcome, DeployTrigger


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.xadd = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.publish_flat = AsyncMock()
    r.publish_message = AsyncMock()
    return r


def _configure_api_mock(api):
    """Configure common API mock methods."""
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


@pytest.fixture
def mock_api():
    api = AsyncMock()
    _configure_api_mock(api)
    with (
        patch("src.consumers.deploy.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
    ):
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


def _job(*, callback_stream=None, user_id="12345", story_id="story-1"):
    return {
        "task_id": "deploy-classify-1",
        "project_id": "proj-1",
        "user_id": user_id,
        "story_id": story_id,
        "callback_stream": callback_stream or "",
        "triggered_by": DeployTrigger.WEBHOOK.value,
    }


# -- _classify_deploy_failure unit tests --


@pytest.mark.asyncio
async def test_classify_returns_code_fix_for_import_error():
    """LLM classifies import errors as CODE_FIX."""
    mock_response = MagicMock()
    mock_response.content = "CODE_FIX"

    with patch("src.consumers.deploy_failure_handler.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy_failure_handler import _classify_deploy_failure

            result = await _classify_deploy_failure("ModuleNotFoundError: No module named 'foo'")
            assert result == "CODE_FIX"


@pytest.mark.asyncio
async def test_classify_returns_retry_for_timeout():
    """LLM classifies timeouts as RETRY."""
    mock_response = MagicMock()
    mock_response.content = "RETRY"

    with patch("src.consumers.deploy_failure_handler.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy_failure_handler import _classify_deploy_failure

            result = await _classify_deploy_failure("Healthcheck timeout after 30s")
            assert result == "RETRY"


@pytest.mark.asyncio
async def test_classify_defaults_to_retry_on_unexpected_response():
    """Unexpected LLM output falls back to RETRY."""
    mock_response = MagicMock()
    mock_response.content = "I'm not sure, maybe both?"

    with patch("src.consumers.deploy_failure_handler.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy_failure_handler import _classify_deploy_failure

            result = await _classify_deploy_failure("some error")
            assert result == "RETRY"


@pytest.mark.asyncio
async def test_classify_defaults_to_retry_on_exception():
    """LLM exception falls back to RETRY."""
    with patch("src.consumers.deploy_failure_handler.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy_failure_handler import _classify_deploy_failure

            result = await _classify_deploy_failure("some error")
            assert result == "RETRY"


@pytest.mark.asyncio
async def test_classify_defaults_to_retry_without_api_key():
    """Missing OPEN_ROUTER_KEY falls back to RETRY."""
    with patch.dict("os.environ", {}, clear=True):
        from src.consumers.deploy_failure_handler import _classify_deploy_failure

        result = await _classify_deploy_failure("some error")
        assert result == "RETRY"


# -- Integration: deploy stores classified outcome in run.result --


def _find_failed_patches(mock_api):
    """Find all PATCH calls that set status=failed and return their result dicts."""
    return [
        c[1]["json"]["result"]
        for c in mock_api.patch.call_args_list
        if c[1].get("json", {}).get("status") == "failed" and "result" in c[1].get("json", {})
    ]


@pytest.mark.asyncio
async def test_smoke_failure_code_fix_stores_outcome(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Smoke failure classified as CODE_FIX → stores deploy_outcome=code_fix."""
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
        }
    )

    with patch(
        "src.consumers.deploy_result_handler._classify_deploy_failure",
        AsyncMock(return_value="CODE_FIX"),
    ):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    results = _find_failed_patches(mock_api)
    assert any(r["deploy_outcome"] == DeployOutcome.CODE_FIX.value for r in results)


@pytest.mark.asyncio
async def test_smoke_failure_retry_stores_outcome(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Smoke failure classified as RETRY → stores deploy_outcome=retry."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {
                "status": "fail",
                "checks": [
                    {"module": "backend", "result": "fail", "detail": "Healthcheck timeout"},
                ],
            },
        }
    )

    with patch(
        "src.consumers.deploy_result_handler._classify_deploy_failure",
        AsyncMock(return_value="RETRY"),
    ):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    results = _find_failed_patches(mock_api)
    assert any(r["deploy_outcome"] == DeployOutcome.RETRY.value for r in results)
    # No engineering message — dispatcher handles routing
    mock_redis.publish_message.assert_not_called()


@pytest.mark.asyncio
async def test_devops_error_retry_stores_outcome(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Devops subgraph errors classified as RETRY → stores deploy_outcome=retry."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": None,
            "errors": ["SSH connection timeout after 60s"],
        }
    )

    with patch(
        "src.consumers.deploy._classify_deploy_failure",
        AsyncMock(return_value="RETRY"),
    ):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    results = _find_failed_patches(mock_api)
    assert any(r["deploy_outcome"] == DeployOutcome.RETRY.value for r in results)


@pytest.mark.asyncio
async def test_devops_error_code_fix_stores_outcome(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Devops errors classified as CODE_FIX → stores deploy_outcome=code_fix."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": None,
            "errors": ["Container exited with code 1: ImportError"],
        }
    )

    with patch(
        "src.consumers.deploy._classify_deploy_failure",
        AsyncMock(return_value="CODE_FIX"),
    ):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    results = _find_failed_patches(mock_api)
    assert any(r["deploy_outcome"] == DeployOutcome.CODE_FIX.value for r in results)
