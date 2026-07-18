"""Test that engineering consumer never updates project.status or service_status."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
import uuid

import pytest

from shared.contracts.dto.project import ProjectDTO, ProjectStatus
from shared.contracts.dto.repository import RepositoryDTO
from src.consumers.engineering import process_engineering_job

_PROJECT_ID = uuid.uuid4()


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


def _make_project(**overrides) -> ProjectDTO:
    base = {
        "id": _PROJECT_ID,
        "title": "test-project",
        "slug": "test-project-0000",
        "status": ProjectStatus.ACTIVE,
        "config": {},
        "owner_id": 1,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ProjectDTO(**base)


def _repo(**overrides) -> RepositoryDTO:
    base = {
        "id": "repo-1",
        "project_id": _PROJECT_ID,
        "name": "test-project",
        "git_url": "https://github.com/org/test-project",
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return RepositoryDTO(**base)


@pytest.mark.asyncio
async def test_engineering_consumer_never_patches_project_status():
    """Engineering consumer should never call api_client.patch with 'status' on projects."""
    api = AsyncMock()
    api.get_project = AsyncMock(return_value=_make_project())
    api.get_primary_repository = AsyncMock(return_value=_repo())
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
    }
    mock_subgraph = AsyncMock()
    mock_subgraph.ainvoke = AsyncMock(return_value=mock_result)

    with (
        patch("src.consumers.engineering.api_client", api),
        patch("src.consumers.engineering_result_handler.api_client", api),
        patch(
            "src.subgraphs.engineering.create_engineering_subgraph",
            return_value=mock_subgraph,
        ),
        patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock),
        patch(
            "src.consumers.engineering_result_handler.publish_callback_event",
            new_callable=AsyncMock,
        ),
        patch("src.consumers.engineering.get_story_worker", return_value=None),
        patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock),
        patch("src.consumers.engineering.resource_allocator_node") as mock_alloc,
    ):
        mock_alloc.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})
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
