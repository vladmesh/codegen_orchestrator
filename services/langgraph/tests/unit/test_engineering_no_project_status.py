"""Test that engineering consumer never updates project.status or service_status."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.project import ProjectStatus, ServiceStatus
from src.consumers.engineering import process_engineering_job


def _make_job_data(**overrides):
    base = {
        "task_id": "eng-test-1",
        "project_id": "proj-1",
        "user_id": "123",
        "callback_stream": "cb:test",
        "action": "feature",
        "description": "add a button",
    }
    base.update(overrides)
    return base


def _make_project(**overrides):
    base = {
        "id": "proj-1",
        "name": "test-project",
        "status": ProjectStatus.ACTIVE.value,
        "service_status": ServiceStatus.RUNNING.value,
        "config": {},
        "owner_id": 1,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_engineering_consumer_never_patches_project_status():
    """Engineering consumer should never call api_client.patch with 'status' on projects."""
    api = AsyncMock()
    api.get_project = AsyncMock(return_value=_make_project())
    api.get_primary_repository = AsyncMock(return_value={"id": "repo-1"})
    api.get_project_allocations = AsyncMock(return_value=[{"server_handle": "s1", "port": 8080}])
    api.get_tasks_by_story = AsyncMock(return_value=[])
    api.get = AsyncMock(return_value={"created_by": "claude"})
    api.patch = AsyncMock()
    api.post = AsyncMock()

    redis = AsyncMock()
    redis.redis = AsyncMock()
    redis.publish_message = AsyncMock()

    # Mock subgraph to return success
    mock_result = {
        "engineering_status": "done",
        "commit_sha": "abc123",
        "worker_id": None,
        "selected_modules": [],
        "test_results": None,
        "allow_no_commit": False,
    }
    mock_subgraph = AsyncMock()
    mock_subgraph.ainvoke = AsyncMock(return_value=mock_result)

    with (
        patch("src.consumers.engineering.api_client", api),
        patch(
            "src.subgraphs.engineering.create_engineering_subgraph",
            return_value=mock_subgraph,
        ),
        patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock),
        patch("src.consumers.engineering.get_story_worker", return_value=None),
        patch(
            "src.consumers.engineering._wait_for_ci_and_fix", return_value=(True, [], False, None)
        ),
        patch("src.consumers.engineering.delete_worker", new_callable=AsyncMock),
        patch("src.consumers.engineering.resource_allocator_node"),
    ):
        result = await process_engineering_job(_make_job_data(), redis)

    assert result["status"] == "success"

    # Verify no api_client.patch call touches project status
    for call in api.patch.call_args_list:
        path = call[0][0] if call[0] else call.kwargs.get("path", "")
        if "projects/" in path:
            json_body = call[1].get("json", {}) if len(call) > 1 else call.kwargs.get("json", {})
            assert "status" not in json_body, (
                f"Engineering consumer should not set project.status, but found: {json_body}"
            )
            assert "service_status" not in json_body, (
                f"Engineering consumer should not set service_status, but found: {json_body}"
            )
