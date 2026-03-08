"""Tests for architect consumer — story decomposition into tasks."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from shared.contracts.queues.architect import ArchitectMessage


class TestDecomposeStory:
    """Test the decompose_story function that calls LLM and creates tasks."""

    @pytest.fixture
    def api_client(self):
        client = AsyncMock()
        # Story response
        client.get_story.return_value = {
            "id": "story-abc",
            "title": "Add user authentication",
            "description": "Implement JWT-based auth with login/register endpoints",
            "project_id": "proj-123",
            "status": "created",
        }
        # Project response
        client.get_project.return_value = {
            "id": "proj-123",
            "name": "my-service",
            "config": {"detailed_spec": "A REST API service"},
        }
        # Existing tasks
        client.get_tasks_by_story.return_value = []
        # Task creation returns task with id
        client.create_task.side_effect = [
            {"id": "task-001", "title": "Task 1"},
            {"id": "task-002", "title": "Task 2"},
            {"id": "task-003", "title": "Task 3"},
        ]
        # Transition returns success
        client.transition_story.return_value = {"status": "in_progress"}
        return client

    @pytest.fixture
    def llm_response(self):
        """Simulated LLM structured response — list of tasks."""
        return [
            {
                "title": "Add User model and migration",
                "description": "Create SQLAlchemy User model with email/password fields",
                "type": "feature",
                "acceptance_criteria": "User table exists with email and hashed_password columns",
                "blocked_by_index": None,
            },
            {
                "title": "Implement register endpoint",
                "description": "POST /auth/register — create user with hashed password",
                "type": "feature",
                "acceptance_criteria": "POST /auth/register creates user and returns 201",
                "blocked_by_index": 0,
            },
            {
                "title": "Implement login endpoint",
                "description": "POST /auth/login — verify credentials, return JWT",
                "type": "feature",
                "acceptance_criteria": "POST /auth/login returns JWT token",
                "blocked_by_index": 0,
            },
        ]

    @pytest.mark.asyncio
    async def test_creates_tasks_from_llm_output(self, api_client, llm_response):
        from src.tasks.architect_consumer import decompose_story

        with patch(
            "src.tasks.architect_consumer.call_llm_decompose",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            msg = ArchitectMessage(
                story_id="story-abc",
                project_id="proj-123",
                user_id="user-1",
            )
            await decompose_story(msg, api_client)

        # Should create 3 tasks
        assert api_client.create_task.call_count == 3

        # First task has no blocker
        first_call = api_client.create_task.call_args_list[0]
        task_data = first_call[0][0]
        assert task_data["story_id"] == "story-abc"
        assert task_data["status"] == "todo"
        assert task_data["blocked_by_task_id"] is None

    @pytest.mark.asyncio
    async def test_sets_dependency_chains(self, api_client, llm_response):
        from src.tasks.architect_consumer import decompose_story

        with patch(
            "src.tasks.architect_consumer.call_llm_decompose",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            msg = ArchitectMessage(
                story_id="story-abc",
                project_id="proj-123",
                user_id="user-1",
            )
            await decompose_story(msg, api_client)

        # Second task (index 1) blocked by first task (index 0 -> task-001)
        second_call = api_client.create_task.call_args_list[1]
        task_data = second_call[0][0]
        assert task_data["blocked_by_task_id"] == "task-001"

        # Third task (index 2) also blocked by first task (index 0 -> task-001)
        third_call = api_client.create_task.call_args_list[2]
        task_data = third_call[0][0]
        assert task_data["blocked_by_task_id"] == "task-001"

    @pytest.mark.asyncio
    async def test_transitions_story_to_in_progress(self, api_client, llm_response):
        from src.tasks.architect_consumer import decompose_story

        with patch(
            "src.tasks.architect_consumer.call_llm_decompose",
            new_callable=AsyncMock,
            return_value=llm_response,
        ):
            msg = ArchitectMessage(
                story_id="story-abc",
                project_id="proj-123",
                user_id="user-1",
            )
            await decompose_story(msg, api_client)

        api_client.transition_story.assert_called_once_with("story-abc", "start")

    @pytest.mark.asyncio
    async def test_handles_empty_llm_response(self, api_client):
        from src.tasks.architect_consumer import decompose_story

        with patch(
            "src.tasks.architect_consumer.call_llm_decompose",
            new_callable=AsyncMock,
            return_value=[],
        ):
            msg = ArchitectMessage(
                story_id="story-abc",
                project_id="proj-123",
                user_id="user-1",
            )
            await decompose_story(msg, api_client)

        # No tasks created
        api_client.create_task.assert_not_called()
        # Story should NOT be transitioned if no tasks
        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_context_to_llm(self, api_client, llm_response):
        from src.tasks.architect_consumer import decompose_story

        with patch(
            "src.tasks.architect_consumer.call_llm_decompose",
            new_callable=AsyncMock,
            return_value=llm_response,
        ) as mock_llm:
            msg = ArchitectMessage(
                story_id="story-abc",
                project_id="proj-123",
                user_id="user-1",
            )
            await decompose_story(msg, api_client)

        # LLM should receive story, project info, and existing tasks
        call_kwargs = mock_llm.call_args[1]
        assert "story" in call_kwargs
        assert "project" in call_kwargs
        assert "existing_tasks" in call_kwargs


class TestArchitectConsumerLoop:
    """Test the consumer loop wiring."""

    @pytest.mark.asyncio
    async def test_processes_valid_message(self):
        from src.tasks.architect_consumer import _process_architect_message

        msg_data = ArchitectMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
        ).model_dump(mode="json")

        # Convert to flat string dict (as Redis returns)
        flat_data = {k: str(v) for k, v in msg_data.items()}

        mock_api = AsyncMock()
        with patch(
            "src.tasks.architect_consumer.decompose_story",
            new_callable=AsyncMock,
        ) as mock_decompose:
            await _process_architect_message(flat_data, mock_api)

        mock_decompose.assert_called_once()
        call_args = mock_decompose.call_args[0]
        assert call_args[0].story_id == "story-abc"

    @pytest.mark.asyncio
    async def test_skips_invalid_message(self):
        from src.tasks.architect_consumer import _process_architect_message

        mock_api = AsyncMock()
        with patch(
            "src.tasks.architect_consumer.decompose_story",
            new_callable=AsyncMock,
        ) as mock_decompose:
            await _process_architect_message({"bad": "data"}, mock_api)

        mock_decompose.assert_not_called()
