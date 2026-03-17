"""Flow test for task dispatcher pipeline: tasks → dispatcher → runs.

Validates task dispatcher picks up tasks and completes stories.
Architect decomposition is now in langgraph service (tested separately).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestDispatcherPipelineFlow:
    """Test dispatcher flow with pre-created tasks (as architect would produce)."""

    @pytest.fixture
    def api_client(self):
        client = AsyncMock()
        return client

    @pytest.fixture
    def redis_client(self):
        client = AsyncMock()
        client.publish_message = AsyncMock()
        client.publish_flat = AsyncMock()
        client.redis = AsyncMock()
        client.redis.hget = AsyncMock(return_value=None)
        client.redis.hdel = AsyncMock()
        client.redis.xadd = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_dispatch_then_complete(self, api_client, redis_client):
        """Tasks created by architect → dispatcher picks them up → story completes."""
        from src.tasks.task_dispatcher import complete_stories, dispatch_todo_tasks

        # --- Phase 1: Dispatcher picks up first unblocked task ---

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

        # --- Phase 2: After task-A completes, task-B gets dispatched ---

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

        # --- Phase 3: After all tasks done, story completes ---

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
        api_client.get_primary_repository.return_value = {
            "git_url": "https://github.com/org/test-project",
        }

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            completed = await complete_stories(api_client, redis_client)

        assert completed == 1
        api_client.transition_story.assert_called_with("story-1", "pr_review")
