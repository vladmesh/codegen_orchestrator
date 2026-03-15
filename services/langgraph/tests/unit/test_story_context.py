"""Unit tests for story context building and inclusion in task messages.

Verifies that:
1. _build_story_context builds compact task list (no descriptions, no events)
2. Current task is excluded (already in TASK.md)
3. Future tasks are marked "do NOT implement"
4. Developer node includes story_context in task messages
5. Consumer passes story_context through EngineeringState
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestBuildStoryContext:
    """Tests for _build_story_context in engineering consumer."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_includes_user_report_when_present(self, mock_api):
        """Story with user_report prepends it to the context."""
        mock_api.get_story = AsyncMock(
            return_value={
                "id": "story-1",
                "user_report": "Images broken on mobile",
            }
        )
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Fix images",
                    "status": "done",
                    "created_at": "2026-03-01T10:00:00",
                },
            ]
        )

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        assert result is not None
        assert "## User Report" in result
        assert "Images broken on mobile" in result
        # User report should appear before tasks
        report_pos = result.index("User Report")
        task_pos = result.index("Fix images")
        assert report_pos < task_pos

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_omits_user_report_when_none(self, mock_api):
        """Story without user_report does not include User Report section."""
        mock_api.get_story = AsyncMock(return_value={"id": "story-1", "user_report": None})
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

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        assert result is not None
        assert "User Report" not in result

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_skips_current_task(self, mock_api):
        """Current task is excluded from story context (already in TASK.md)."""
        mock_api.get_story = AsyncMock(return_value={"id": "story-1", "user_report": None})
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Create User model",
                    "status": "done",
                    "created_at": "2026-03-01T10:00:00",
                },
                {
                    "id": "task-2",
                    "title": "Add API endpoint",
                    "status": "in_dev",
                    "created_at": "2026-03-02T10:00:00",
                },
            ]
        )

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1", current_task_id="task-2")

        assert result is not None
        assert "Create User model" in result
        assert "done" in result
        # Current task should NOT appear in context
        assert "Add API endpoint" not in result
        assert "CURRENT" not in result

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_done_tasks_show_old_tasks_reference(self, mock_api):
        """Completed tasks reference .story/old_tasks/ directory."""
        mock_api.get_story = AsyncMock(return_value={"id": "story-1", "user_report": None})
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Create model",
                    "status": "done",
                    "created_at": "2026-03-01T10:00:00",
                },
            ]
        )

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        assert "old_tasks" in result

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_future_tasks_marked_do_not_implement(self, mock_api):
        """Backlog/todo tasks are marked 'do NOT implement'."""
        mock_api.get_story = AsyncMock(return_value={"id": "story-1", "user_report": None})
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Current task",
                    "status": "in_dev",
                    "created_at": "2026-03-01T10:00:00",
                },
                {
                    "id": "task-2",
                    "title": "Future backlog task",
                    "status": "backlog",
                    "description": "Secret implementation details",
                    "created_at": "2026-03-02T10:00:00",
                },
                {
                    "id": "task-3",
                    "title": "Future todo task",
                    "status": "todo",
                    "created_at": "2026-03-03T10:00:00",
                },
            ]
        )

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1", current_task_id="task-1")

        assert "do NOT implement" in result
        assert "Future backlog task" in result
        assert "Future todo task" in result
        # Descriptions should NOT be included
        assert "Secret implementation details" not in result

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_no_descriptions_included(self, mock_api):
        """Task descriptions are never included in story context."""
        mock_api.get_story = AsyncMock(return_value={"id": "story-1", "user_report": None})
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Some task",
                    "status": "done",
                    "description": "Detailed description that should not appear",
                    "created_at": "2026-03-01T10:00:00",
                },
            ]
        )

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        assert "Detailed description" not in result

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_no_events_fetched(self, mock_api):
        """Events are not fetched — get_task_events should not be called."""
        mock_api.get_story = AsyncMock(return_value={"id": "story-1", "user_report": None})
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
        mock_api.get_task_events = AsyncMock()

        from src.consumers.engineering import _build_story_context

        await _build_story_context("story-1")
        mock_api.get_task_events.assert_not_called()

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
    async def test_sorts_tasks_chronologically(self, mock_api):
        """Tasks are sorted by created_at."""
        mock_api.get_story = AsyncMock(return_value={"id": "story-1", "user_report": None})
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

        from src.consumers.engineering import _build_story_context

        result = await _build_story_context("story-1")
        first_pos = result.index("First task")
        second_pos = result.index("Second task")
        assert first_pos < second_pos


class TestDeveloperNodeStoryContext:
    """Tests that developer node includes story_context in task messages."""

    def test_feature_task_includes_story_context(self):
        """_build_feature_task includes story context section."""
        from src.nodes.developer import DeveloperNode

        node = DeveloperNode()
        ctx = "- ~~Create User model~~ — done (see .story/old_tasks/)"
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
        ctx = "- ~~Scaffold project~~ — done (see .story/old_tasks/)"
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
        ctx = "- ~~Previous work~~ — done (see .story/old_tasks/)"

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
            "story_context": "- ~~Create model~~ — done (see .story/old_tasks/)",
            "repo_id": None,
            "errors": [],
        }
        await node.run(state)

        call_kwargs = mock_spawn.call_args[1]
        assert "Create model" in call_kwargs["task_content"]
        assert "Story Context" in call_kwargs["task_content"]

    @pytest.mark.asyncio
    @patch("src.nodes.developer.request_spawn", new_callable=AsyncMock)
    @patch("src.nodes.developer.api_client")
    @patch("src.nodes.developer.GitHubAppClient")
    async def test_story_md_passed_to_spawn(self, mock_github_cls, mock_api, mock_spawn):
        """story_md from state flows to request_spawn as keyword argument."""
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
        story_md_content = "# Story: Build weather bot\n\n## Tasks\n1. Create API"
        state = {
            "project_spec": {
                "id": "proj-1",
                "name": "test-project",
                "status": "active",
                "config": {"modules": ["backend"], "description": "API"},
            },
            "action": "feature",
            "description": "Add endpoint",
            "story_context": None,
            "story_md": story_md_content,
            "repo_id": None,
            "errors": [],
        }
        await node.run(state)

        call_kwargs = mock_spawn.call_args[1]
        assert call_kwargs["story_md"] == story_md_content


class TestBuildStoryMd:
    """Tests for _build_story_md in engineering consumer."""

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_builds_story_md_with_tasks(self, mock_api):
        """Generates STORY.md with goal and task list."""
        mock_api.get_story = AsyncMock(
            return_value={
                "id": "story-1",
                "title": "Build weather bot",
                "description": "Full weather API + Telegram bot",
                "user_report": None,
            }
        )
        mock_api.get_tasks_by_story = AsyncMock(
            return_value=[
                {
                    "id": "task-1",
                    "title": "Create API endpoint",
                    "status": "done",
                    "created_at": "2026-03-01T10:00:00",
                },
                {
                    "id": "task-2",
                    "title": "Create Telegram bot",
                    "status": "in_dev",
                    "created_at": "2026-03-02T10:00:00",
                },
            ]
        )

        from src.consumers.engineering import _build_story_md

        result = await _build_story_md("story-1", current_task_id="task-2")
        assert result is not None
        assert "# Story: Build weather bot" in result
        assert "Full weather API" in result
        assert "~~Create API endpoint~~ — done" in result
        assert "**Create Telegram bot** — current" in result
        assert "README.md" in result

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_returns_none_on_api_failure(self, mock_api):
        """API failure returns None."""
        mock_api.get_story = AsyncMock(side_effect=Exception("API down"))

        from src.consumers.engineering import _build_story_md

        result = await _build_story_md("story-fail")
        assert result is None

    @pytest.mark.asyncio
    @patch("src.consumers.engineering.api_client")
    async def test_includes_user_report(self, mock_api):
        """Story with user_report includes it in STORY.md."""
        mock_api.get_story = AsyncMock(
            return_value={
                "id": "story-1",
                "title": "Fix bugs",
                "description": None,
                "user_report": "Images broken on mobile",
            }
        )
        mock_api.get_tasks_by_story = AsyncMock(return_value=[])

        from src.consumers.engineering import _build_story_md

        result = await _build_story_md("story-1")
        assert result is not None
        assert "User Report" in result
        assert "Images broken on mobile" in result
