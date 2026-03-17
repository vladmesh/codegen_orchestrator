"""Unit test — two-task story lifecycle within langgraph consumer.

Verifies that process_engineering_job correctly looks up, passes, and preserves
worker_id across consecutive tasks in the same story. All external deps mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.project import ProjectStatus


def _project(**overrides):
    base = {
        "id": "proj-1",
        "name": "test-project",
        "config": {"modules": ["backend"]},
        "status": ProjectStatus.ACTIVE.value,
    }
    base.update(overrides)
    return base


class TestTwoTaskStoryLifecycle:
    """Consumer handles two sequential tasks in one story correctly."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.get_story_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering_result_handler.delete_worker", new_callable=AsyncMock)
    @patch("src.consumers.engineering._handle_engineering_success", new_callable=AsyncMock)
    @patch("src.subgraphs.engineering.create_engineering_subgraph")
    @patch("src.consumers.engineering.resource_allocator_node")
    @patch("src.consumers.engineering.api_client")
    @patch("src.consumers.engineering.publish_callback_event", new_callable=AsyncMock)
    async def test_first_task_no_worker_second_task_reuses(
        self,
        mock_publish,
        mock_api,
        mock_resource,
        mock_create_graph,
        mock_handle_success,
        mock_delete,
        mock_get_worker,
    ):
        """First task: no worker in registry → None passed to subgraph.
        Second task: worker found → worker_id passed to subgraph.
        Worker never deleted between tasks."""
        mock_api.patch = AsyncMock()
        mock_api.get_project = AsyncMock(return_value=_project())
        mock_api.get_tasks_by_story = AsyncMock(return_value=[])
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_api.post = AsyncMock()
        mock_resource.run = AsyncMock(return_value={"allocated_resources": {}, "errors": []})
        mock_handle_success.return_value = {"status": "success"}

        redis_mock = MagicMock()
        redis_mock.redis = MagicMock()
        redis_mock.redis.xadd = AsyncMock()

        from src.consumers.engineering import process_engineering_job

        # --- Task 1: No existing worker ---
        mock_get_worker.return_value = None

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "abc123",
                "worker_id": "dev-story1-abc",
            }
        )
        mock_create_graph.return_value = mock_graph

        await process_engineering_job(
            {
                "task_id": "eng-task1",
                "project_id": "proj-1",
                "user_id": "u-1",
                "action": "feature",
                "description": "Create User model",
                "story_id": "story-1",
                "planning_task_id": "task-1",
                "skip_deploy": True,
            },
            redis_mock,
        )

        # Looked up worker (found none)
        mock_get_worker.assert_called_with(redis_mock.redis, "story-1")
        # Subgraph got no worker_id
        invoke_args_1 = mock_graph.ainvoke.call_args[0][0]
        assert invoke_args_1.get("worker_id") is None
        # story_id passed to handle_success
        assert mock_handle_success.call_args.kwargs["story_id"] == "story-1"

        # --- Task 2: Existing worker ---
        mock_get_worker.reset_mock()
        mock_handle_success.reset_mock()
        mock_create_graph.reset_mock()

        mock_get_worker.return_value = "dev-story1-abc"

        mock_graph_2 = AsyncMock()
        mock_graph_2.ainvoke = AsyncMock(
            return_value={
                "engineering_status": "done",
                "commit_sha": "def456",
                "worker_id": "dev-story1-abc",
            }
        )
        mock_create_graph.return_value = mock_graph_2

        await process_engineering_job(
            {
                "task_id": "eng-task2",
                "project_id": "proj-1",
                "user_id": "u-1",
                "action": "feature",
                "description": "Add API endpoint",
                "story_id": "story-1",
                "planning_task_id": "task-2",
                "skip_deploy": True,
            },
            redis_mock,
        )

        # Looked up worker (found it)
        mock_get_worker.assert_called_with(redis_mock.redis, "story-1")
        # Subgraph got worker_id from registry
        invoke_args_2 = mock_graph_2.ainvoke.call_args[0][0]
        assert invoke_args_2["worker_id"] == "dev-story1-abc"
        # story_id passed to handle_success
        assert mock_handle_success.call_args.kwargs["story_id"] == "story-1"
        # Worker never deleted (kept alive for story)
        mock_delete.assert_not_called()
