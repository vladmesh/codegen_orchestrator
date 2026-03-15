"""Tests for pipeline supervisor — stuck detection, retry, and fail-fast."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

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
    client.publish = AsyncMock()
    client.redis = AsyncMock()
    client.redis.hget = AsyncMock(return_value=None)  # No story worker by default
    client.redis.hdel = AsyncMock()
    # _redis is used by supervise_stuck_stories for retry counter persistence
    client._redis = AsyncMock()
    client._redis.get = AsyncMock(return_value=None)  # No retries by default
    client._redis.set = AsyncMock()
    client._redis.delete = AsyncMock()
    return client


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestSuperviseStuckStories:
    """Detect stories stuck in 'created' and retry architect or fail."""

    @pytest.mark.asyncio
    async def test_retries_stuck_story(self, api_client, redis_client):
        """Story stuck in created > threshold → republish to architect:queue."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                {
                    "id": "story-1",
                    "project_id": "proj-1",
                    "user_id": "u-1",
                    "created_at": _iso(old),
                }
            ]
            if status == "created"
            else []  # no in_progress stories
        )
        # No tasks yet = architect hasn't run
        api_client.get_tasks_by_story.return_value = []
        # No previous retry events
        api_client.get_task_events.side_effect = []

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 1
        assert result["failed"] == 0
        redis_client.publish_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_recent_story(self, api_client, redis_client):
        """Story created recently → no action."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        recent = datetime.now(UTC) - timedelta(minutes=1)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                {
                    "id": "story-1",
                    "project_id": "proj-1",
                    "user_id": "u-1",
                    "created_at": _iso(recent),
                }
            ]
            if status == "created"
            else []
        )

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_story_with_tasks(self, api_client, redis_client):
        """Story in created but has tasks → architect ran, skip."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                {
                    "id": "story-1",
                    "project_id": "proj-1",
                    "user_id": "u-1",
                    "created_at": _iso(old),
                }
            ]
            if status == "created"
            else []
        )
        api_client.get_tasks_by_story.return_value = [{"id": "task-1"}]

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_fails_story_after_max_retries(self, api_client, redis_client):
        """Story retried 3 times → fail the story."""
        from src.tasks.task_dispatcher import (
            STORY_MAX_ARCHITECT_RETRIES,
            supervise_stuck_stories,
        )

        old_enough = datetime.now(UTC) - timedelta(minutes=10 * (STORY_MAX_ARCHITECT_RETRIES + 1))
        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                {
                    "id": "story-1",
                    "project_id": "proj-1",
                    "user_id": "u-1",
                    "created_at": _iso(old_enough),
                    "updated_at": _iso(old),
                }
            ]
            if status == "created"
            else []  # no in_progress stories
        )
        api_client.get_tasks_by_story.return_value = []
        api_client.fail_story.return_value = {}

        # Simulate retry count already at max in Redis
        redis_client._redis.get.return_value = str(STORY_MAX_ARCHITECT_RETRIES)

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_skips_created_story_when_project_has_active(self, api_client, redis_client):
        """Story stuck in created but project has an in_progress story → skip."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                {
                    "id": "story-queued",
                    "project_id": "proj-1",
                    "user_id": "u-1",
                    "created_at": _iso(old),
                }
            ]
            if status == "created"
            else [{"id": "story-active", "project_id": "proj-1"}]
        )
        api_client.get_tasks_by_story.return_value = []

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        assert result["failed"] == 0
        redis_client.publish_message.assert_not_called()


class TestCompleteStoriesTriggersNext:
    """After completing a story, trigger the next queued story for the same project."""

    @pytest.mark.asyncio
    async def test_triggers_next_created_story(self, api_client, redis_client):
        """Story completed → next created story for same project published to architect."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                {
                    "id": "story-done",
                    "project_id": "proj-1",
                    "user_id": "u-1",
                }
            ]
            if status == "in_progress"
            else [
                {
                    "id": "story-next",
                    "project_id": "proj-1",
                    "user_id": "u-2",
                    "priority": 0,
                    "created_at": _iso(datetime.now(UTC)),
                }
            ]
        )
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "done"},
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}
        api_client.get_primary_repository.return_value = {
            "git_url": "https://github.com/org/test-project",
        }

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.task_dispatcher.GitHubAppClient", return_value=mock_github):
            completed = await complete_stories(api_client, redis_client)

        assert completed == 1
        # Should publish architect message for next story
        from shared.queues import ARCHITECT_QUEUE

        arch_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ARCHITECT_QUEUE
        ]
        assert len(arch_calls) == 1
        assert arch_calls[0][0][1].story_id == "story-next"

    @pytest.mark.asyncio
    async def test_no_next_story_when_none_queued(self, api_client, redis_client):
        """Story completed but no created stories for project → no architect trigger."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                {
                    "id": "story-done",
                    "project_id": "proj-1",
                    "user_id": "u-1",
                }
            ]
            if status == "in_progress"
            else []
        )
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "done"},
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}
        api_client.get_primary_repository.return_value = {
            "git_url": "https://github.com/org/test-project",
        }

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.task_dispatcher.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        from shared.queues import ARCHITECT_QUEUE

        arch_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ARCHITECT_QUEUE
        ]
        assert len(arch_calls) == 0


class TestSuperviseFailedTasks:
    """Detect failed tasks and retry or escalate."""

    @pytest.mark.asyncio
    async def test_retries_failed_task(self, api_client, redis_client):
        """Failed task with iterations left → reopen to todo."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "story_id": "story-1",
                "current_iteration": 0,
                "max_iterations": 3,
            }
        ]
        api_client.transition_task.return_value = {}
        api_client.update_task.return_value = {}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 1
        # Should transition: failed → backlog → todo
        calls = api_client.transition_task.call_args_list
        assert len(calls) == 2  # noqa: PLR2004
        assert calls[0].args == ("task-1", "backlog", "supervisor")
        assert calls[1].args == ("task-1", "todo", "supervisor")
        # Should increment current_iteration
        api_client.update_task.assert_called_once_with("task-1", {"current_iteration": 1})

    @pytest.mark.asyncio
    async def test_fails_story_when_retries_exhausted(self, api_client, redis_client):
        """Failed task at max iterations → fail story."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "story_id": "story-1",
                "current_iteration": 3,
                "max_iterations": 3,
            }
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "failed"},
            {"id": "task-2", "status": "in_dev"},
        ]
        api_client.transition_task.return_value = {}
        api_client.fail_story.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        # Should notify user
        redis_client.publish_flat.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_task_without_story(self, api_client, redis_client):
        """Failed task without story_id → skip (standalone task)."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "story_id": None,
                "current_iteration": 0,
                "max_iterations": 3,
            }
        ]

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 0
        assert result["failed"] == 0
        api_client.transition_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancels_sibling_tasks_on_terminal_failure(self, api_client, redis_client):
        """When story fails, cancel remaining non-failed sibling tasks."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "story_id": "story-1",
                "current_iteration": 3,
                "max_iterations": 3,
            }
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "failed"},
            {"id": "task-2", "status": "todo"},
        ]
        api_client.transition_task.return_value = {}
        api_client.fail_story.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        await supervise_failed_tasks(api_client, redis_client)

        # task-2 should be cancelled
        cancel_calls = [
            c for c in api_client.transition_task.call_args_list if c.args[1] == "cancelled"
        ]
        assert len(cancel_calls) == 1
        assert cancel_calls[0].args[0] == "task-2"


class TestSuperviseStuckTasks:
    """Detect tasks stuck in in_dev and fail them."""

    @pytest.mark.asyncio
    async def test_fails_stuck_in_dev_task(self, api_client, redis_client):
        """Task in in_dev > threshold → transition to failed."""
        from src.tasks.task_dispatcher import supervise_stuck_tasks

        old = datetime.now(UTC) - timedelta(minutes=45)
        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "story_id": "story-1",
                "updated_at": _iso(old),
            }
        ]
        api_client.transition_task.return_value = {}

        result = await supervise_stuck_tasks(api_client, redis_client)

        assert result["timed_out"] == 1
        api_client.transition_task.assert_called_once_with("task-1", "failed", "supervisor")

    @pytest.mark.asyncio
    async def test_skips_recent_in_dev_task(self, api_client, redis_client):
        """Task recently updated → no action."""
        from src.tasks.task_dispatcher import supervise_stuck_tasks

        recent = datetime.now(UTC) - timedelta(minutes=5)
        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "story_id": "story-1",
                "updated_at": _iso(recent),
            }
        ]

        result = await supervise_stuck_tasks(api_client, redis_client)

        assert result["timed_out"] == 0
        api_client.transition_task.assert_not_called()


class TestStoryWorkerCleanup:
    """Cleanup story workers on story complete/fail."""

    @pytest.mark.asyncio
    async def test_cleanup_on_story_complete(self, api_client, redis_client):
        """Story completed → worker container deleted, registry cleared."""
        from src.tasks.task_dispatcher import STORY_WORKERS_KEY, complete_stories

        api_client.get_stories_by_status.return_value = [
            {"id": "story-1", "project_id": "proj-1", "user_id": "u-1"}
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "done"},
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}
        api_client.get_primary_repository.return_value = {
            "git_url": "https://github.com/org/test-project",
        }

        # Story has a worker registered
        redis_client.redis.hget.return_value = b"dev-story-worker"

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.task_dispatcher.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        # Should lookup worker
        redis_client.redis.hget.assert_called_with(STORY_WORKERS_KEY, "story-1")
        # Should send delete command
        redis_client.publish.assert_called_once()
        # Should clear registry
        redis_client.redis.hdel.assert_called_with(STORY_WORKERS_KEY, "story-1")

    @pytest.mark.asyncio
    async def test_no_cleanup_when_no_worker(self, api_client, redis_client):
        """Story completed but no worker registered → no cleanup."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            {"id": "story-1", "project_id": "proj-1", "user_id": "u-1"}
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "done"},
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}
        api_client.get_primary_repository.return_value = {
            "git_url": "https://github.com/org/test-project",
        }

        # No worker registered
        redis_client.redis.hget.return_value = None

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.task_dispatcher.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        # Should not send delete command or clear registry
        redis_client.publish.assert_not_called()
        redis_client.redis.hdel.assert_not_called()

    @pytest.mark.asyncio
    async def test_cleanup_on_story_failure(self, api_client, redis_client):
        """Story failed (task retries exhausted) → worker cleaned up."""
        from src.tasks.task_dispatcher import STORY_WORKERS_KEY, supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            {
                "id": "task-1",
                "story_id": "story-1",
                "current_iteration": 3,
                "max_iterations": 3,
            }
        ]
        api_client.get_tasks_by_story.return_value = [
            {"id": "task-1", "status": "failed"},
        ]
        api_client.transition_task.return_value = {}
        api_client.fail_story.return_value = {}
        api_client.get_story.return_value = {"user_id": "u-1"}

        # Story has a worker registered
        redis_client.redis.hget.return_value = b"dev-failed-worker"

        await supervise_failed_tasks(api_client, redis_client)

        # Should cleanup worker
        redis_client.redis.hget.assert_called_with(STORY_WORKERS_KEY, "story-1")
        redis_client.publish.assert_called_once()
        redis_client.redis.hdel.assert_called_with(STORY_WORKERS_KEY, "story-1")
