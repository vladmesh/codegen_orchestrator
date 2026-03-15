"""Unit tests for deploy failure LLM classifier (CODE vs INFRA)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.deploy import DeployTrigger
from shared.queues import ENGINEERING_QUEUE


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
    r.publish_message = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    with patch("src.consumers.deploy.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get = AsyncMock(return_value=[])
        api.get_project = AsyncMock(
            return_value={
                "name": "my-project",
                "config": {"modules": ["backend"]},
            }
        )
        api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/my-project"}
        )
        api.transition_story = AsyncMock()
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
async def test_classify_returns_code_for_import_error():
    """LLM classifies import errors as CODE."""
    mock_response = MagicMock()
    mock_response.content = "CODE"

    with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy import _classify_deploy_failure

            result = await _classify_deploy_failure("ModuleNotFoundError: No module named 'foo'")
            assert result == "CODE"


@pytest.mark.asyncio
async def test_classify_returns_infra_for_timeout():
    """LLM classifies timeouts as INFRA."""
    mock_response = MagicMock()
    mock_response.content = "INFRA"

    with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy import _classify_deploy_failure

            result = await _classify_deploy_failure("Healthcheck timeout after 30s")
            assert result == "INFRA"


@pytest.mark.asyncio
async def test_classify_defaults_to_code_on_unexpected_response():
    """Unexpected LLM output falls back to CODE."""
    mock_response = MagicMock()
    mock_response.content = "I'm not sure, maybe both?"

    with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy import _classify_deploy_failure

            result = await _classify_deploy_failure("some error")
            assert result == "CODE"


@pytest.mark.asyncio
async def test_classify_defaults_to_code_on_exception():
    """LLM exception falls back to CODE."""
    with patch("src.consumers.deploy.ChatOpenAI") as mock_llm_cls:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM down"))
        mock_llm_cls.return_value = mock_llm

        with patch.dict("os.environ", {"OPEN_ROUTER_KEY": "test-key"}):
            from src.consumers.deploy import _classify_deploy_failure

            result = await _classify_deploy_failure("some error")
            assert result == "CODE"


@pytest.mark.asyncio
async def test_classify_defaults_to_code_without_api_key():
    """Missing OPEN_ROUTER_KEY falls back to CODE."""
    with patch.dict("os.environ", {}, clear=True):
        from src.consumers.deploy import _classify_deploy_failure

        result = await _classify_deploy_failure("some error")
        assert result == "CODE"


# -- Integration: smoke failure with classification --


@pytest.mark.asyncio
async def test_smoke_failure_code_dispatches_to_engineering(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Smoke failure classified as CODE → dispatches fix task to engineering."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {
                "status": "fail",
                "checks": [
                    {
                        "module": "backend",
                        "result": "fail",
                        "detail": "HTTP 500 Internal Server Error",
                    },
                ],
            },
        }
    )

    with patch("src.consumers.deploy._classify_deploy_failure", AsyncMock(return_value="CODE")):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    # Engineering fix task should be published
    engineering_calls = [
        c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
    ]
    assert len(engineering_calls) == 1


@pytest.mark.asyncio
async def test_smoke_failure_infra_retries_deploy(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Smoke failure classified as INFRA → no engineering task, story retried."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {
                "status": "fail",
                "checks": [
                    {
                        "module": "backend",
                        "result": "fail",
                        "detail": "Healthcheck timeout after 30s",
                    },
                ],
            },
        }
    )

    with patch("src.consumers.deploy._classify_deploy_failure", AsyncMock(return_value="INFRA")):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    # No engineering fix task
    engineering_calls = [
        c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
    ]
    assert len(engineering_calls) == 0
    # Story should be rolled back to "start" (retry counter incremented)
    mock_redis.redis.incr.assert_called()


@pytest.mark.asyncio
async def test_devops_error_infra_skips_engineering(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Devops subgraph errors classified as INFRA → no engineering task."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": None,
            "errors": ["SSH connection timeout after 60s"],
        }
    )

    with patch("src.consumers.deploy._classify_deploy_failure", AsyncMock(return_value="INFRA")):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    engineering_calls = [
        c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
    ]
    assert len(engineering_calls) == 0


@pytest.mark.asyncio
async def test_devops_error_code_dispatches_to_engineering(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Devops subgraph errors classified as CODE → dispatches to engineering."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": None,
            "errors": ["Container exited with code 1: ImportError: cannot import name 'app'"],
        }
    )

    with patch("src.consumers.deploy._classify_deploy_failure", AsyncMock(return_value="CODE")):
        from src.consumers.deploy import process_deploy_job

        result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "failed"
    engineering_calls = [
        c for c in mock_redis.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
    ]
    assert len(engineering_calls) == 1
