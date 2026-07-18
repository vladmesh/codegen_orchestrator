"""Test that engineering consumer routes gave_up status to _handle_worker_gave_up."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.dto.engineering import EngineeringStatus
from tests.unit.factories import make_project, make_repository, make_story

_PATCH = "src.consumers.engineering"


def _make_job_data(**overrides) -> dict:
    defaults = {
        "task_id": "eng-test-1",
        "project_id": "proj-1",
        "user_id": "123",
        "callback_stream": "cb:123",
        "action": "fix",
        "description": "Fix deploy error",
        "story_id": "story-1",
        "planning_task_id": "task-plan-1",
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def mock_api():
    with patch(f"{_PATCH}.api_client") as api:
        api.patch = AsyncMock()
        api.post = AsyncMock()
        api.get_project = AsyncMock(
            return_value=make_project(
                name="test",
                status="active",
                config={"modules": ["backend"], "description": "test"},
            )
        )
        api.get_primary_repository = AsyncMock(return_value=make_repository())
        api.get_tasks_by_story = AsyncMock(return_value=[])
        api.get_story = AsyncMock(return_value=make_story(title="test story"))
        yield api


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.redis = AsyncMock()
    return redis


class TestGaveUpRouting:
    """Engineering consumer routes gave_up to _handle_worker_gave_up."""

    @pytest.mark.asyncio
    @patch(f"{_PATCH}.publish_callback_event", new_callable=AsyncMock)
    @patch(f"{_PATCH}._handle_worker_gave_up", new_callable=AsyncMock)
    @patch(f"{_PATCH}._resolve_allocations", new_callable=AsyncMock)
    @patch(f"{_PATCH}.get_story_worker", new_callable=AsyncMock)
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    async def test_gave_up_routes_to_handler(
        self,
        mock_create_subgraph,
        mock_get_worker,
        mock_allocations,
        mock_handle_gave_up,
        mock_publish,
        mock_api,
        mock_redis,
    ):
        """When subgraph returns gave_up, call _handle_worker_gave_up."""
        mock_allocations.return_value = {"backend": {"server_ip": "1.2.3.4"}}
        mock_get_worker.return_value = None

        # Subgraph returns gave_up with gave_up_reason
        mock_subgraph = AsyncMock()
        mock_subgraph.ainvoke.return_value = {
            "engineering_status": EngineeringStatus.GAVE_UP,
            "gave_up_reason": "Port conflict is not a code issue",
            "worker_report": None,
            "errors": ["Worker gave up: Port conflict is not a code issue"],
        }
        mock_create_subgraph.return_value = mock_subgraph

        mock_handle_gave_up.return_value = {
            "status": "gave_up",
            "reason": "Port conflict is not a code issue",
            "finished_at": datetime.now(UTC).isoformat(),
        }

        from src.consumers.engineering import process_engineering_job

        result = await process_engineering_job(_make_job_data(), mock_redis)

        mock_handle_gave_up.assert_called_once()
        call_kwargs = mock_handle_gave_up.call_args[1]
        assert call_kwargs["task_id"] == "eng-test-1"
        assert call_kwargs["reason"] == "Port conflict is not a code issue"
        assert call_kwargs["planning_task_id"] == "task-plan-1"
        assert call_kwargs["story_id"] == "story-1"
        assert result["status"] == "gave_up"
