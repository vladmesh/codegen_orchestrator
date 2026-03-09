"""Unit tests for story context building and inclusion in task messages.

Verifies that:
1. _build_story_context fetches tasks + events and formats them
2. Developer node includes story_context in task messages
3. Consumer passes story_context through EngineeringState
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestBuildStoryContext:
    """Tests for _build_story_context in engineering consumer."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_builds_context_with_tasks_and_events(self, mock_api):
        """Story with completed tasks + events produces formatted context."""
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Create User model",
                    "status": "done",
                    "description": "Implement User model with email/password",
                    "created_at": "2026-03-01T10:00:00",
                },
                {
                    "id": "task-2",
                    "title": "Add API endpoint",
                    "status": "in_dev",
                    "description": "Add GET /users endpoint",
                    "created_at": "2026-03-02T10:00:00",
                },
            ]
        )
        mock_api.get_task_events = AsyncMock(
            side_effect=[
                # Events for task-1
                [
                    {
                        "event_type": "status_change",
                        "from_status": "backlog",
                        "to_status": "in_dev",
                        "actor": "dispatcher",
                        "details": {},
                    },
                    {
                        "event_type": "note",
                        "actor": "engineering-worker",
                        "details": {"action": "step_done", "commit_sha": "abc123"},
                    },
                    {
                        "event_type": "status_change",
                        "from_status": "in_dev",
                        "to_status": "done",
                        "actor": "engineering-worker",
                        "details": {},
                    },
                ],
                # Events for task-2
                [
                    {
                        "event_type": "status_change",
                        "from_status": "backlog",
                        "to_status": "in_dev",
                        "actor": "dispatcher",
                        "details": {},
                    },
                ],
            ]
        )

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1", current_task_id="task-2")

        assert result is not None
        assert "Create User model" in result
        assert "[done]" in result
        assert "Add API endpoint" in result
        assert "[in_dev]" in result
        assert "CURRENT" in result  # task-2 is current
        assert "abc123" in result  # commit sha from event details

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_returns_none_for_empty_story(self, mock_api):
        """Story with no tasks returns None."""
        mock_api.get_tasks_by_story = AsyncMock(return_value=[])

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-empty")
        assert result is None

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_handles_api_failure_gracefully(self, mock_api):
        """API failure returns None instead of raising."""
        mock_api.get_tasks_by_story = AsyncMock(side_effect=Exception("API down"))

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-fail")
        assert result is None

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_handles_event_fetch_failure(self, mock_api):
        """Event fetch failure for a task doesn't break the whole context."""
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Some task",
                    "status": "done",
                    "created_at": "2026-03-01T10:00:00",
                },
            ]
        )
        mock_api.get_task_events = AsyncMock(side_effect=Exception("Event API down"))

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        assert result is not None
        assert "Some task" in result

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_sorts_tasks_chronologically(self, mock_api):
        """Tasks are sorted by created_at."""
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-b",
                    "title": "Second task",
                    "status": "in_dev",
                    "created_at": "2026-03-02T10:00:00",
                },
                {
                    "id": "task-a",
                    "title": "First task",
                    "status": "done",
                    "created_at": "2026-03-01T10:00:00",
                },
            ]
        )
        mock_api.get_task_events = AsyncMock(return_value=[])

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        first_pos = result.index("First task")
        second_pos = result.index("Second task")
        assert first_pos < second_pos

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_truncates_long_descriptions(self, mock_api):
        """Long descriptions are truncated to 300 chars."""
        long_desc = "x" * 500
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Task",
                    "status": "done",
                    "description": long_desc,
                    "created_at": "2026-03-01T10:00:00",
                },
            ]
        )
        mock_api.get_task_events = AsyncMock(return_value=[])

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        # Description should be truncated
        assert "x" * 301 not in result


class TestDeveloperNodeStoryContext:
    """Tests that developer node includes story_context in task messages."""

    def test_feature_task_includes_story_context(self):
        """_build_feature_task includes story context section."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        ctx = "### Task: Create User model [done]\nEvents:\n  - [status_change] done"
        task_md = node._build_feature_task(
            project_name="test-project",
            description="An API",
            modules=["backend"],
            action="feature",
            feature_description="Add search",
            project_spec={},
            story_context=ctx,
        )
        assert "Story Context" in task_md
        assert "Create User model" in task_md
        assert "do NOT redo completed work" in task_md

    def test_feature_task_without_story_context(self):
        """_build_feature_task without story context has no story section."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        task_md = node._build_feature_task(
            project_name="test-project",
            description="An API",
            modules=["backend"],
            action="feature",
            feature_description="Add search",
            project_spec={},
            story_context=None,
        )
        assert "Story Context" not in task_md

    def test_create_task_includes_story_context(self):
        """_build_create_task includes story context section."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        ctx = "### Task: Scaffold project [done]"
        task_md = node._build_create_task(
            project_name="test-project",
            description="Build something",
            modules=["backend"],
            project_spec={},
            story_context=ctx,
        )
        assert "Story Context" in task_md
        assert "Scaffold project" in task_md

    def test_build_task_message_passes_story_context(self):
        """_build_task_message forwards story_context to underlying builders."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        ctx = "### Task: Previous work [done]"

        # Test with feature action
        task_md = node._build_task_message(
            project_name="test-project",
            description="An API",
            modules=["backend"],
            repo_full_name="org/test",
            project_spec={},
            action="feature",
            feature_description="New feature",
            story_context=ctx,
        )
        assert "Previous work" in task_md

        # Test with create action
        task_md = node._build_task_message(
            project_name="test-project",
            description="An API",
            modules=["backend"],
            repo_full_name="org/test",
            project_spec={},
            action="create",
            story_context=ctx,
        )
        assert "Previous work" in task_md

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_story_context_in_spawn_task_content(self, mock_github_cls, mock_api, mock_spawn):
        """story_context from state flows into task_content sent to worker."""
        from src.clients.worker_spawner import SpawnResult

        mock_github_cls.return_value.get_token = AsyncMock(return_value="ghs_fake")
        mock_api.get_project = AsyncMock(return_value=None)
        mock_api.get_primary_repository = AsyncMock(
            return_value={"id": "repo-1", "git_url": "https://github.com/org/test-project"}
        )
        mock_spawn.return_value = SpawnResult(
            request_id="req-1",
            success=True,
            exit_code=0,
            output="Done",
            commit_sha="abc123",
        )

        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        state = {
            "project_spec": {
                "id": "proj-1",
                "name": "test-project",
                "status": "active",
                "config": {"modules": ["backend"], "description": "API"},
            },
            "action": "feature",
            "description": "Add endpoint",
            "story_context": "### Task: Create model [done]\nCommit: abc",
            "repo_id": None,
            "errors": [],
        }
        await node.run(state)

        call_kwargs = mock_spawn.call_args[1]
        assert "Create model" in call_kwargs["task_content"]
        assert "Story Context" in call_kwargs["task_content"]
