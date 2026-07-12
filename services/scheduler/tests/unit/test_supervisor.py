"""Tests for pipeline supervisor — stuck detection, retry, and fail-fast.

Run-routing tests (DEPLOYING/TESTING stories) live in
`test_supervisor_run_routing.py`; shared DTO factories in `_run_routing_factories`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from _run_routing_factories import _make_repo, _make_story, _make_task
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


class TestSuperviseStuckStories:
    """Detect stories stuck in 'created' and retry architect or fail."""

    @pytest.mark.asyncio
    async def test_retries_stuck_story(self, api_client, redis_client):
        """Story stuck in created > threshold -> republish to architect:queue."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1", project_id="00000000-0000-0000-0000-000000000001", created_at=old
                )
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
        """Story created recently -> no action."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        recent = datetime.now(UTC) - timedelta(minutes=1)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1",
                    project_id="00000000-0000-0000-0000-000000000001",
                    created_at=recent,
                )
            ]
            if status == "created"
            else []
        )

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_story_with_tasks(self, api_client, redis_client):
        """Story in created but has tasks -> architect ran, skip."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1", project_id="00000000-0000-0000-0000-000000000001", created_at=old
                )
            ]
            if status == "created"
            else []
        )
        api_client.get_tasks_by_story.return_value = [_make_task(id="task-1")]

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_fails_story_after_max_retries(self, api_client, redis_client):
        """Story retried 3 times -> fail the story."""
        from src.tasks.supervisor import _max_architect_retries
        from src.tasks.task_dispatcher import supervise_stuck_stories

        max_retries = _max_architect_retries()
        old_enough = datetime.now(UTC) - timedelta(minutes=10 * (max_retries + 1))
        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1",
                    project_id="00000000-0000-0000-0000-000000000001",
                    created_at=old_enough,
                    updated_at=old,
                )
            ]
            if status == "created"
            else []  # no in_progress stories
        )
        api_client.get_tasks_by_story.return_value = []
        api_client.fail_story.return_value = {}

        # Simulate retry count already at max in Redis
        redis_client._redis.get.return_value = str(max_retries)

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_skips_created_story_when_project_has_active(self, api_client, redis_client):
        """Story stuck in created but project has an in_progress story -> skip."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.side_effect = lambda status: (
            [_make_story(id="story-queued", project_id=proj_id, created_at=old)]
            if status == "created"
            else [_make_story(id="story-active", project_id=proj_id, status="in_progress")]
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
        """Story completed -> next created story for same project published to architect."""
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.side_effect = lambda status: (
            [_make_story(id="story-done", project_id=proj_id, status="in_progress")]
            if status == "in_progress"
            else [
                _make_story(
                    id="story-next",
                    project_id=proj_id,
                    status="created",
                    priority=0,
                    created_at=datetime.now(UTC),
                )
            ]
        )
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-done", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
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
        """Story completed but no created stories for project -> no architect trigger."""
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.side_effect = lambda status: (
            [_make_story(id="story-done", project_id=proj_id, status="in_progress")]
            if status == "in_progress"
            else []
        )
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-done", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        from shared.queues import ARCHITECT_QUEUE

        arch_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ARCHITECT_QUEUE
        ]
        assert len(arch_calls) == 0


class TestSuperviseFailedTasks:
    """Detect failed tasks and retry or escalate to WHR."""

    @pytest.mark.asyncio
    async def test_retries_failed_task(self, api_client, redis_client):
        """Failed task with iterations left -> reopen to todo."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="failed",
                current_iteration=0,
                max_iterations=3,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.update_task.return_value = {}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 1
        # Should transition: failed -> backlog -> todo
        calls = api_client.transition_task.call_args_list
        assert len(calls) == 2  # noqa: PLR2004
        assert calls[0].args == ("task-1", "backlog", "supervisor")
        assert calls[1].args == ("task-1", "todo", "supervisor")
        # Should increment current_iteration
        api_client.update_task.assert_called_once_with("task-1", {"current_iteration": 1})

    @pytest.mark.asyncio
    async def test_escalates_to_whr_when_retries_exhausted(self, api_client, redis_client):
        """Failed task at max iterations -> escalate to waiting_human_review."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="failed",
                current_iteration=3,
                max_iterations=3,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.transition_story.return_value = {}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["escalated"] == 1
        # Task should be transitioned to WHR
        api_client.transition_task.assert_called_once_with(
            "task-1", "waiting_human_review", "supervisor"
        )
        # Story should also be transitioned to WHR
        api_client.transition_story.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_task_without_story(self, api_client, redis_client):
        """Failed task without story_id -> skip (standalone task)."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id=None,
                status="failed",
                current_iteration=0,
                max_iterations=3,
            )
        ]

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 0
        assert result["escalated"] == 0
        api_client.transition_task.assert_not_called()


class TestSuperviseStuckTasks:
    """Detect tasks stuck in in_dev and fail them."""

    @pytest.mark.asyncio
    async def test_fails_stuck_in_dev_task(self, api_client, redis_client):
        """Task in in_dev > threshold -> transition to failed."""
        from src.tasks.task_dispatcher import supervise_stuck_tasks

        old = datetime.now(UTC) - timedelta(minutes=45)
        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="in_dev",
                updated_at=old,
            )
        ]
        api_client.transition_task.return_value = {}

        result = await supervise_stuck_tasks(api_client, redis_client)

        assert result["timed_out"] == 1
        api_client.transition_task.assert_called_once_with("task-1", "failed", "supervisor")

    @pytest.mark.asyncio
    async def test_skips_recent_in_dev_task(self, api_client, redis_client):
        """Task recently updated -> no action."""
        from src.tasks.task_dispatcher import supervise_stuck_tasks

        recent = datetime.now(UTC) - timedelta(minutes=5)
        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="in_dev",
                updated_at=recent,
            )
        ]

        result = await supervise_stuck_tasks(api_client, redis_client)

        assert result["timed_out"] == 0
        api_client.transition_task.assert_not_called()


class TestStoryWorkerCleanup:
    """Cleanup story workers on story complete/fail."""

    @pytest.mark.asyncio
    async def test_cleanup_on_story_complete(self, api_client, redis_client):
        """Story completed -> worker container deleted, registry cleared."""
        from shared.queues import STORY_WORKERS_KEY
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", project_id=proj_id, status="in_progress")
        ]
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-1", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        # Story has a worker registered
        redis_client.redis.hget.return_value = b"dev-story-worker"

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        # Should lookup worker
        redis_client.redis.hget.assert_called_with(STORY_WORKERS_KEY, "story-1")
        # Should send delete command
        redis_client.publish.assert_called_once()
        # Should clear registry
        redis_client.redis.hdel.assert_called_with(STORY_WORKERS_KEY, "story-1")

    @pytest.mark.asyncio
    async def test_no_cleanup_when_no_worker(self, api_client, redis_client):
        """Story completed but no worker registered -> no cleanup."""
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", project_id=proj_id, status="in_progress")
        ]
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-1", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        # No worker registered
        redis_client.redis.hget.return_value = None

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        # Should not send delete command or clear registry
        redis_client.publish.assert_not_called()
        redis_client.redis.hdel.assert_not_called()

    @pytest.mark.asyncio
    async def test_escalation_transitions_story_to_whr(self, api_client, redis_client):
        """Task retries exhausted -> story transitioned to WHR (not failed)."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="failed",
                current_iteration=3,
                max_iterations=3,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.transition_story.return_value = {}

        await supervise_failed_tasks(api_client, redis_client)

        # Story should NOT be failed — just transitioned to WHR
        api_client.fail_story.assert_not_called()
        api_client.transition_story.assert_called_once()
