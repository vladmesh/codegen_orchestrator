"""Flow test for architect pipeline: story → architect → tasks → dispatcher → runs.

Validates the full pipeline with all external dependencies mocked.
Tests the interaction between architect consumer and task dispatcher.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from shared.contracts.queues.architect import ArchitectMessage


class TestArchitectPipelineFlow:
    """End-to-end flow test with mocked API + LLM."""

    @pytest.fixture
    def api_client(self):
        client = AsyncMock()
        return client

    @pytest.fixture
    def redis_client(self):
        client = AsyncMock()
        client.publish_message = AsyncMock()
        client.publish_flat = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_full_flow_architect_then_dispatch(self, api_client, redis_client):
        """Story → architect decomposes into 2 tasks → dispatcher picks them up."""
        from unittest.mock import patch

        from src.tasks.architect_consumer import decompose_story
        from src.tasks.task_dispatcher import complete_stories, dispatch_todo_tasks

        # --- Phase 1: Architect decomposes story ---

        api_client.get_story.return_value = {
            "id": "story-1",
            "title": "Add auth",
            "description": "JWT auth with login",
            "project_id": "proj-1",
            "user_id": "u-1",
        }
        api_client.get_project.return_value = {
            "id": "proj-1",
            "name": "my-api",
            "config": {"detailed_spec": "REST API"},
        }
        api_client.get_tasks_by_story.return_value = []
        api_client.create_task.side_effect = [
            {"id": "task-A"},
            {"id": "task-B"},
        ]
        api_client.transition_story.return_value = {}

        llm_response = [
            {
                "title": "Add User model",
                "description": "SQLAlchemy model",
                "type": "feature",
                "acceptance_criteria": "User table exists",
                "blocked_by_index": None,
            },
            {
                "title": "Add login endpoint",
                "description": "POST /auth/login",
                "type": "feature",
                "acceptance_criteria": "Returns JWT",
                "blocked_by_index": 0,
            },
        ]

        msg = ArchitectMessage(story_id="story-1", project_id="proj-1", user_id="u-1")
        with patch(
            "src.tasks.architect_consumer.call_llm_decompose",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            await decompose_story(msg, api_client)

        # Architect created 2 tasks
        assert api_client.create_task.call_count == 2

        # Second task blocked by first
        second_task_data = api_client.create_task.call_args_list[1][0][0]
        assert second_task_data["blocked_by_task_id"] == "task-A"

        # Story transitioned to in_progress
        api_client.transition_story.assert_called_with("story-1", "start")

        # --- Phase 2: Dispatcher picks up first task ---

        api_client.reset_mock()
        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-A",
                "title": "Add User model",
                "description": "SQLAlchemy model",
                "type": "feature",
                "project_id": "proj-1",
                "story_id": "story-1",
                "blocked_by_task_id": None,
                "status": "todo",
            },
            {
                "id": "task-B",
                "title": "Add login endpoint",
                "description": "POST /auth/login",
                "type": "feature",
                "project_id": "proj-1",
                "story_id": "story-1",
                "blocked_by_task_id": "task-A",
                "status": "todo",
            },
        ]
        api_client.get_task.return_value = {"id": "task-A", "status": "todo"}
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-A", "status": "todo"},
            {"id": "task-B", "status": "todo"},
        ]
        api_client.get_task_events.return_value = []
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        # Only task-A dispatched (task-B blocked by task-A which isn't done)
        assert dispatched == 1
        api_client.transition_task.assert_called_once_with("task-A", "in_dev", "dispatcher")

        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.planning_task_id == "task-A"
        assert eng_msg.skip_deploy is True

        # --- Phase 3: After task-A completes, task-B gets dispatched ---

        api_client.reset_mock()
        redis_client.reset_mock()
        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-B",
                "title": "Add login endpoint",
                "description": "POST /auth/login",
                "type": "feature",
                "project_id": "proj-1",
                "story_id": "story-1",
                "blocked_by_task_id": "task-A",
                "status": "todo",
            },
        ]
        api_client.get_task.return_value = {"id": "task-A", "status": "done"}
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-A", "status": "done"},
            {"id": "task-B", "status": "todo"},
        ]
        api_client.get_task_events.return_value = [
            {
                "event_type": "iteration_end",
                "details": {"commit_sha": "abc", "summary": "User model created"},
            }
        ]
        api_client.create_run.return_value = {"id": "run-2"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.planning_task_id == "task-B"
        # Should include context from task-A
        assert "User model created" in eng_msg.description

        # --- Phase 4: After all tasks done, story completes ---

        api_client.reset_mock()
        redis_client.reset_mock()
        api_client.get_stories_by_status.return_value = [
            {"id": "story-1", "project_id": "proj-1", "user_id": "u-1"}
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-A", "status": "done"},
            {"id": "task-B", "status": "done"},
        ]
        api_client.transition_story.return_value = {}

        completed = await complete_stories(api_client, redis_client)

        assert completed == 1
        api_client.transition_story.assert_called_with("story-1", "complete")
        # Deploy triggered
        assert redis_client.publish_message.call_count >= 1
