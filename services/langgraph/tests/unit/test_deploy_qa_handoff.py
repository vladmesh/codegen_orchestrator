"""Unit tests for deploy success outcome storage.

After successful deploy+smoke, deploy worker stores deploy_outcome=SUCCESS
in run.result. Story transition and QA handoff are handled by the dispatcher.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.deploy import DeployOutcome, DeployTrigger
from tests.unit.factories import make_project, make_repository


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.set = AsyncMock(return_value=True)  # lock acquired
    r.redis.delete = AsyncMock()
    r.publish_flat = AsyncMock()
    r.publish_message = AsyncMock()
    return r


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.patch = AsyncMock()
    api.get = AsyncMock(return_value=[])
    api.get_project = AsyncMock(
        return_value=make_project(
            name="weather-bot",
            config={"modules": ["backend"]},
        )
    )
    api.get_primary_repository = AsyncMock(
        return_value=make_repository(
            git_url="https://github.com/org/weather-bot",
        )
    )
    with (
        patch("src.consumers.deploy.api_client", api),
        patch("src.consumers.deploy_result_handler.api_client", api),
        patch("src.consumers.deploy_failure_handler.api_client", api),
        patch("src.consumers.deploy_precheck.api_client", api),
    ):
        yield api


@pytest.fixture
def mock_allocations():
    mock_fn = AsyncMock(return_value={"srv:8000": {"server_ip": "1.2.3.4", "port": 8000}})
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


def _job(*, story_id="story-1", user_id="12345"):
    return {
        "task_id": "deploy-qa-1",
        "project_id": "proj-1",
        "user_id": user_id,
        "story_id": story_id,
        "callback_stream": "",
        "triggered_by": DeployTrigger.ENGINEERING.value,
        "head_sha": "a" * 40,
    }


@pytest.mark.asyncio
async def test_deploy_success_stores_outcome(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Successful deploy stores deploy_outcome=SUCCESS in run.result."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {"status": "pass", "checks": []},
            "application_id": 1,
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(), mock_redis)

    assert result["status"] == "success"
    # Run should be patched with success outcome
    patch_calls = mock_api.patch.call_args_list
    # Find the final patch (status=completed)
    completed_patch = [c for c in patch_calls if c[1].get("json", {}).get("status") == "completed"]
    assert len(completed_patch) == 1
    run_result = completed_patch[0][1]["json"]["result"]
    assert run_result["deploy_outcome"] == DeployOutcome.SUCCESS.value
    assert run_result["deployed_url"] == "http://1.2.3.4:8080"
    assert run_result["application_id"] == 1


@pytest.mark.asyncio
async def test_deploy_success_does_not_transition_story(
    mock_redis, mock_api, mock_allocations, mock_devops_subgraph
):
    """Deploy worker must NOT transition story — dispatcher does that."""
    mock_devops_subgraph.ainvoke = AsyncMock(
        return_value={
            "deployed_url": "http://1.2.3.4:8080",
            "deployment_result": {},
            "smoke_result": {"status": "pass", "checks": []},
            "application_id": 1,
        }
    )

    from src.consumers.deploy import process_deploy_job

    await process_deploy_job(_job(), mock_redis)

    # No story transition, no QA message
    mock_api.transition_story.assert_not_called()
    mock_redis.publish_message.assert_not_called()


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
            "application_id": 1,
        }
    )

    from src.consumers.deploy import process_deploy_job

    result = await process_deploy_job(_job(story_id=""), mock_redis)

    assert result["status"] == "success"
    mock_redis.publish_message.assert_not_called()
