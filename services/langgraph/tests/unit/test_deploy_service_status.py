"""Test that deploy consumer only sets service_status, never project.status."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.project import ServiceStatus
from src.consumers.deploy import (
    _handle_deploy_failure,
    _handle_deploy_success,
    _handle_smoke_failure,
)


@pytest.fixture
def mock_api():
    api = AsyncMock()
    api.patch = AsyncMock()
    api.transition_story = AsyncMock()
    return api


@pytest.fixture
def mock_redis():
    r = AsyncMock()
    r.redis = AsyncMock()
    r.redis.incr = AsyncMock(return_value=1)
    r.redis.expire = AsyncMock()
    r.publish_message = AsyncMock()
    return r


@pytest.mark.asyncio
async def test_deploy_success_sets_service_status_running(mock_api, mock_redis):
    with patch("src.consumers.deploy.api_client", mock_api):
        await _handle_deploy_success(
            result={"deployed_url": "https://example.com", "deployment_result": {}},
            smoke_result=None,
            task_id="deploy-1",
            project_id="proj-1",
            project={"name": "test"},
            callback_stream="cb:test",
            user_id="123",
            story_id="story-1",
            redis=mock_redis,
        )

    # Find the patch call to projects/
    project_patches = [c for c in mock_api.patch.call_args_list if "projects/" in str(c[0][0])]
    assert len(project_patches) == 1
    json_body = project_patches[0][1]["json"]
    assert json_body == {"service_status": ServiceStatus.RUNNING.value}
    assert "status" not in json_body


@pytest.mark.asyncio
async def test_deploy_failure_sets_service_status_down(mock_api, mock_redis):
    with (
        patch("src.consumers.deploy.api_client", mock_api),
        patch("src.consumers.deploy.publish_callback_event", new_callable=AsyncMock),
    ):
        await _handle_deploy_failure(
            task_id="deploy-1",
            project_id="proj-1",
            error_msg="containers crashed",
            story_id="story-1",
            callback_stream="cb:test",
            user_id="123",
            redis=mock_redis,
            rollback_project=True,
        )

    project_patches = [c for c in mock_api.patch.call_args_list if "projects/" in str(c[0][0])]
    assert len(project_patches) == 1
    json_body = project_patches[0][1]["json"]
    assert json_body == {"service_status": ServiceStatus.DOWN.value}
    assert "status" not in json_body


@pytest.mark.asyncio
async def test_smoke_failure_sets_service_status_degraded(mock_api, mock_redis):
    from shared.contracts.queues.deploy import DeployMessage, DeployTrigger

    msg = DeployMessage(
        task_id="deploy-1",
        project_id="proj-1",
        user_id="123",
        callback_stream="cb:test",
        triggered_by=DeployTrigger.ENGINEERING,
        action="create",
    )

    with (
        patch("src.consumers.deploy.api_client", mock_api),
        patch("src.consumers.deploy.publish_callback_event", new_callable=AsyncMock),
        patch("src.consumers.deploy._redispatch_to_engineering", new_callable=AsyncMock),
    ):
        await _handle_smoke_failure(
            result={"deployed_url": "https://example.com"},
            smoke_result={
                "status": "fail",
                "checks": [{"module": "api", "result": "fail", "detail": "500"}],
            },
            task_id="deploy-1",
            project_id="proj-1",
            project_name="test",
            callback_stream="cb:test",
            user_id="123",
            story_id="story-1",
            redis=mock_redis,
            msg=msg,
        )

    project_patches = [c for c in mock_api.patch.call_args_list if "projects/" in str(c[0][0])]
    assert len(project_patches) == 1
    json_body = project_patches[0][1]["json"]
    assert json_body == {"service_status": ServiceStatus.DEGRADED.value}
    assert "status" not in json_body
