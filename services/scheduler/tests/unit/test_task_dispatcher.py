"""Tests for task dispatcher — dispatches todo tasks and completes stories."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def api_client():
    client = AsyncMock()
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish_flat = AsyncMock()
    return client


class TestDispatchTodoTasks:
    """Dispatch unblocked todo tasks to engineering queue."""

    @pytest.mark.asyncio
    async def test_dispatches_unblocked_task(self, api_client, redis_client):
        """Task with no blocker gets a run created and published."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "title": "Add user model",
                "description": "Create User SQLAlchemy model",
                "type": "feature",
                "project_id": "proj-1",
                "story_id": "story-1",
                "blocked_by_task_id": None,
                "status": "todo",
            }
        ]
        api_client.get_task_events.return_value = []
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        await dispatch_todo_tasks(api_client, redis_client)

        # Should create a run
        api_client.create_run.assert_called_once()
        run_data = api_client.create_run.call_args[0][0]
        assert run_data["type"] == "engineering"
        assert run_data["project_id"] == "proj-1"

        # Should publish to engineering queue
        redis_client.publish_message.assert_called_once()

        # Should transition task to in_dev
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_skips_blocked_task(self, api_client, redis_client):
        """Task blocked by non-done task is skipped."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-2",
                "title": "Add API endpoint",
                "description": "REST endpoint",
                "type": "feature",
                "project_id": "proj-1",
                "story_id": "story-1",
                "blocked_by_task_id": "task-1",
                "status": "todo",
            }
        ]
        api_client.get_task.return_value = {"id": "task-1", "status": "in_dev"}

        await dispatch_todo_tasks(api_client, redis_client)

        # Should NOT create a run
        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_task_when_blocker_done(self, api_client, redis_client):
        """Task whose blocker is done gets dispatched."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-2",
                "title": "Add API endpoint",
                "description": "REST endpoint",
                "type": "feature",
                "project_id": "proj-1",
                "story_id": "story-1",
                "blocked_by_task_id": "task-1",
                "status": "todo",
            }
        ]
        api_client.get_task.return_value = {"id": "task-1", "status": "done"}
        api_client.get_task_events.return_value = [
            {
                "event_type": "iteration_end",
                "details": {"commit_sha": "abc", "summary": "Done"},
            }
        ]
        api_client.create_run.return_value = {"id": "run-2"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        await dispatch_todo_tasks(api_client, redis_client)

        api_client.create_run.assert_called_once()
        redis_client.publish_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_includes_cumulative_context(self, api_client, redis_client):
        """Dispatched task includes context from completed sibling tasks."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-2",
                "title": "Add API endpoint",
                "description": "REST endpoint",
                "type": "feature",
                "project_id": "proj-1",
                "story_id": "story-1",
                "blocked_by_task_id": "task-1",
                "status": "todo",
            }
        ]
        api_client.get_task.return_value = {"id": "task-1", "status": "done"}
        # Sibling tasks for story-1: task-1 (done) and task-2 (todo)
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "done"},
            {"id": "task-2", "status": "todo"},
        ]
        # Events for task-1 (the done sibling)
        api_client.get_task_events.return_value = [
            {
                "event_type": "iteration_end",
                "details": {
                    "commit_sha": "abc123",
                    "summary": "Created User model with email field",
                },
            }
        ]
        api_client.create_run.return_value = {"id": "run-2"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        await dispatch_todo_tasks(api_client, redis_client)

        # The engineering message should have enriched description
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert "User model" in eng_msg.description
        assert eng_msg.planning_task_id == "task-2"


class TestCompleteStories:
    """Complete stories when all tasks are done."""

    @pytest.mark.asyncio
    async def test_completes_story_when_all_tasks_done(self, api_client, redis_client):
        """Story with all tasks done → completed + deploy triggered."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            {"id": "story-1", "project_id": "proj-1", "user_id": "u-1"}
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "done"},
            {"id": "task-2", "status": "done"},
        ]
        api_client.transition_story.return_value = {}

        await complete_stories(api_client, redis_client)

        # Should complete story
        api_client.transition_story.assert_called_once_with("story-1", "complete")

        # Should publish deploy message
        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if "deploy" in str(c).lower()
        ]
        assert len(deploy_calls) == 1

    @pytest.mark.asyncio
    async def test_no_complete_when_tasks_pending(self, api_client, redis_client):
        """Story with pending tasks → no action."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            {"id": "story-1", "project_id": "proj-1", "user_id": "u-1"}
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "done"},
            {"id": "task-2", "status": "in_dev"},
        ]

        await complete_stories(api_client, redis_client)

        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_complete_when_no_tasks(self, api_client, redis_client):
        """Story with zero tasks → no action (architect may not have run yet)."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            {"id": "story-1", "project_id": "proj-1", "user_id": "u-1"}
        ]
        api_client.get_tasks_by_story.return_value = []

        await complete_stories(api_client, redis_client)

        api_client.transition_story.assert_not_called()
