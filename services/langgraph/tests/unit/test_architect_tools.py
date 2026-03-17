"""Unit tests for architect tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tests.unit.factories import make_project, make_story, make_task


@pytest.fixture
def mock_api():
    with patch("src.agents.architect.tools.api_client") as api:
        api.get_story = AsyncMock(
            return_value=make_story(id="story-abc", title="Add auth", status="created")
        )
        api.get_project = AsyncMock(
            return_value=make_project(name="my-api", config={"detailed_spec": "REST"})
        )
        api.get_tasks_by_story = AsyncMock(return_value=[])
        api.create_task = AsyncMock(return_value=make_task(id="task-new", title="New task"))
        api.transition_story = AsyncMock(return_value=make_story(status="in_progress"))
        yield api


class TestGetStoryTool:
    @pytest.mark.asyncio
    async def test_returns_story(self, mock_api):
        from src.agents.architect.tools import get_story

        result = await get_story.ainvoke({"story_id": "story-abc"})

        assert result["id"] == "story-abc"
        mock_api.get_story.assert_called_once_with("story-abc")


class TestGetProjectSpecTool:
    @pytest.mark.asyncio
    async def test_returns_project(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        assert result["name"] == "my-api"
        mock_api.get_project.assert_called_once_with("proj-1")

    @pytest.mark.asyncio
    async def test_surfaces_tree_from_config(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = make_project(
            name="my-api",
            config={"tree": ".\n├── src/\n│   └── main.py", "secrets": {"DB": "xxx"}},
            project_spec={"modules": ["backend"]},
        )

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        assert result["tree"] == ".\n├── src/\n│   └── main.py"
        assert result["project_spec"] == {"modules": ["backend"]}
        assert "secrets" not in result.get("config", {})

    @pytest.mark.asyncio
    async def test_handles_missing_tree(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = make_project(
            name="my-api",
            config={},
            project_spec=None,
        )

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        assert result.get("tree") is None
        assert result.get("project_spec") is None

    @pytest.mark.asyncio
    async def test_strips_noisy_config_fields(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = make_project(
            name="my-api",
            config={
                "tree": ".",
                "secrets": {"key": "val"},
                "env_hints": ["hint"],
                "detailed_spec": "important spec",
            },
        )

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        config = result.get("config", {})
        assert "secrets" not in config
        assert "env_hints" not in config
        assert config.get("detailed_spec") == "important spec"

    @pytest.mark.asyncio
    async def test_returns_error_when_not_found(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = None
        result = await get_project_spec.ainvoke({"project_id": "proj-missing"})

        assert "error" in result


class TestGetTasksByStoryTool:
    @pytest.mark.asyncio
    async def test_returns_tasks(self, mock_api):
        from src.agents.architect.tools import get_tasks_by_story

        result = await get_tasks_by_story.ainvoke({"story_id": "story-abc"})

        assert result == []
        mock_api.get_tasks_by_story.assert_called_once_with("story-abc")


class TestCreateTaskTool:
    @pytest.fixture(autouse=True)
    def _reset_chain(self):
        from src.agents.architect.tools import reset_task_chain

        reset_task_chain()
        yield
        reset_task_chain()

    @pytest.mark.asyncio
    async def test_creates_task(self, mock_api):
        from src.agents.architect.tools import create_task

        result = await create_task.ainvoke(
            {
                "title": "Add User model",
                "description": "Create model",
                "type": "feature",
                "acceptance_criteria": "Model exists",
                "story_id": "story-abc",
                "project_id": "proj-1",
            }
        )

        assert result["id"] == "task-new"
        call_args = mock_api.create_task.call_args[0][0]
        assert call_args["title"] == "Add User model"
        assert call_args["status"] == "todo"
        assert call_args["created_by"] == "architect"
        # First task has no dependency
        assert call_args["blocked_by_task_id"] is None

    @pytest.mark.asyncio
    async def test_auto_chains_tasks(self, mock_api):
        """Second task is automatically blocked by the first."""
        from src.agents.architect.tools import create_task

        mock_api.create_task = AsyncMock(
            side_effect=[
                make_task(id="task-001", title="First"),
                make_task(id="task-002", title="Second"),
            ]
        )

        await create_task.ainvoke(
            {
                "title": "First task",
                "description": "Do first thing",
                "type": "feature",
                "acceptance_criteria": "Done",
                "story_id": "story-abc",
                "project_id": "proj-1",
            }
        )
        await create_task.ainvoke(
            {
                "title": "Second task",
                "description": "Do second thing",
                "type": "feature",
                "acceptance_criteria": "Done",
                "story_id": "story-abc",
                "project_id": "proj-1",
            }
        )

        first_call = mock_api.create_task.call_args_list[0][0][0]
        second_call = mock_api.create_task.call_args_list[1][0][0]
        assert first_call["blocked_by_task_id"] is None
        assert second_call["blocked_by_task_id"] == "task-001"


class TestTransitionStoryTool:
    @pytest.mark.asyncio
    async def test_transitions_story(self, mock_api):
        from src.agents.architect.tools import transition_story

        result = await transition_story.ainvoke({"story_id": "story-abc", "action": "start"})

        assert result["status"] == "in_progress"
        mock_api.transition_story.assert_called_once_with("story-abc", "start")
