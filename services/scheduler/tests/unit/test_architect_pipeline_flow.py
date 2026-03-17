"""Flow test for task dispatcher pipeline: tasks → dispatcher → runs.

Validates task dispatcher picks up tasks and completes stories.
Architect decomposition is now in langgraph service (tested separately).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO

_NOW = datetime.now(UTC)
_PROJ_ID = "00000000-0000-0000-0000-000000000001"


def _task(*, id: str, status: str = "todo", **overrides) -> TaskDTO:
    defaults = {
        "id": id,
        "project_id": UUID(_PROJ_ID),
        "type": "feature",
        "title": id,
        "description": "",
        "status": status,
        "priority": 0,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "story_id": "story-1",
        "blocked_by_task_id": None,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return TaskDTO(**defaults)


def _story(*, id: str, status: str = "in_progress", **overrides) -> StoryDTO:
    defaults = {
        "project_id": UUID(_PROJ_ID),
        "title": id,
        "type": "product",
        "status": status,
        "priority": 0,
        "created_by": "system",
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return StoryDTO(id=id, **defaults)


def _repo(**overrides) -> RepositoryDTO:
    defaults = {
        "id": "repo-1",
        "project_id": UUID(_PROJ_ID),
        "name": "test-project",
        "git_url": "https://github.com/org/test-project",
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": _NOW,
    }
    defaults.update(overrides)
    return RepositoryDTO(**defaults)


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
            _task(
                id="task-A",
                title="Add User model",
                description="SQLAlchemy model",
                blocked_by_task_id=None,
            ),
            _task(
                id="task-B",
                title="Add login endpoint",
                description="POST /auth/login",
                blocked_by_task_id="task-A",
            ),
        ]
        api_client.get_task.return_value = _task(id="task-A")
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-A"),
            _task(id="task-B"),
        ]
        api_client.get_task_events.return_value = []
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1")

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
            _task(
                id="task-B",
                title="Add login endpoint",
                description="POST /auth/login",
                blocked_by_task_id="task-A",
            ),
        ]
        api_client.get_task.return_value = _task(id="task-A", status="done")
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-A", status="done"),
            _task(id="task-B"),
        ]
        api_client.get_task_events.return_value = [
            TaskEventDTO(
                id=1,
                task_id="task-A",
                event_type="iteration_end",
                details={"commit_sha": "abc", "summary": "User model created"},
                actor="worker",
                created_at=_NOW,
            ),
        ]
        api_client.create_run.return_value = {"id": "run-2"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1")

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
            _story(id="story-1"),
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-A", status="done"),
            _task(id="task-B", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_primary_repository.return_value = _repo()

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            completed = await complete_stories(api_client, redis_client)

        assert completed == 1
        api_client.transition_story.assert_called_with("story-1", "pr_review")
