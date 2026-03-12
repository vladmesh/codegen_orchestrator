"""Unit tests for architect consumer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.queues.architect import ArchitectMessage


class TestProcessArchitectJob:
    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()

    @pytest.fixture
    def valid_job_data(self):
        msg = ArchitectMessage(
            story_id="story-abc",
            project_id="proj-123",
            user_id="user-1",
        )
        return msg.model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_skips_invalid_message(self, mock_redis):
        from src.consumers.architect import process_architect_job

        result = await process_architect_job({"bad": "data"}, mock_redis)

        assert result["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_fails_without_llm_config(self, mock_redis, valid_job_data):
        with patch("src.consumers.architect.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                architect_llm_api_key=None,
                architect_llm_model=None,
                architect_llm_base_url=None,
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        assert "not set" in result["error"]

    @pytest.mark.asyncio
    async def test_invokes_graph_on_valid_message(self, mock_redis, valid_job_data):
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.append_ci_check_task", new_callable=AsyncMock),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_graph.ainvoke.assert_called_once()

        # Verify state passed to graph
        call_args = mock_graph.ainvoke.call_args[0][0]
        assert call_args["story_id"] == "story-abc"
        assert call_args["project_id"] == "proj-123"
        assert len(call_args["messages"]) == 1

    @pytest.mark.asyncio
    async def test_reopen_message_includes_user_report(self, mock_redis):
        """Reopen messages include user_report in the initial state."""
        reopen_data = ArchitectMessage(
            story_id="story-reopen",
            project_id="proj-123",
            user_id="user-1",
            is_reopen=True,
            user_report="Images broken on mobile",
        ).model_dump(mode="json")

        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.append_ci_check_task", new_callable=AsyncMock),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(reopen_data, mock_redis)

        assert result["status"] == "success"
        call_args = mock_graph.ainvoke.call_args[0][0]
        user_msg = call_args["messages"][0]["content"]
        assert "REOPEN" in user_msg
        assert "Images broken on mobile" in user_msg
        assert "get_tasks_by_story" in user_msg

    @pytest.mark.asyncio
    async def test_normal_message_no_reopen_context(self, mock_redis, valid_job_data):
        """Normal messages use standard decomposition prompt."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.append_ci_check_task", new_callable=AsyncMock),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        call_args = mock_graph.ainvoke.call_args[0][0]
        user_msg = call_args["messages"][0]["content"]
        assert "REOPEN" not in user_msg
        assert "Decompose story" in user_msg

    @pytest.mark.asyncio
    async def test_handles_graph_error(self, mock_redis, valid_job_data):
        mock_graph = AsyncMock()
        mock_graph.ainvoke.side_effect = RuntimeError("LLM timeout")

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.append_ci_check_task", new_callable=AsyncMock),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        assert "LLM timeout" in result["error"]


class TestProcessArchitectJobIntegration:
    """Integration-style test: full flow with mocked graph + mocked API."""

    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()

    @pytest.fixture
    def valid_job_data(self):
        msg = ArchitectMessage(
            story_id="story-int",
            project_id="proj-int",
            user_id="user-1",
        )
        return msg.model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_full_flow_creates_tasks_and_ci(self, mock_redis, valid_job_data):
        """Graph creates architect tasks → consumer appends CI task."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        created_tasks = []

        async def mock_create_task(data):
            task_id = f"task-{len(created_tasks):03d}"
            task = {"id": task_id, **data}
            created_tasks.append(task)
            return task

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.api_client") as mock_api,
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="key",
                architect_llm_model="model",
                architect_llm_base_url="http://test",
            )
            # Simulate architect having already created 2 tasks via tools
            mock_api.get_tasks_by_story = AsyncMock(
                return_value=[
                    {
                        "id": "task-arch-1",
                        "blocked_by_task_id": None,
                        "created_by": "architect",
                    },
                    {
                        "id": "task-arch-2",
                        "blocked_by_task_id": "task-arch-1",
                        "created_by": "architect",
                    },
                ]
            )
            mock_api.create_task = AsyncMock(side_effect=mock_create_task)

            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        # CI task was created
        assert len(created_tasks) == 1
        ci_task = created_tasks[0]
        assert ci_task["created_by"] == "system"
        assert ci_task["blocked_by_task_id"] == "task-arch-2"
        assert "test" in ci_task["title"].lower() or "ci" in ci_task["title"].lower()

    @pytest.mark.asyncio
    async def test_full_flow_no_tasks_no_ci(self, mock_redis, valid_job_data):
        """If architect creates no tasks (duplicates), no CI task is appended."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": []}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.api_client") as mock_api,
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="key",
                architect_llm_model="model",
                architect_llm_base_url="http://test",
            )
            mock_api.get_tasks_by_story = AsyncMock(return_value=[])
            mock_api.create_task = AsyncMock()

            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_api.create_task.assert_not_called()


class TestAppendCiCheckTask:
    @pytest.fixture
    def mock_api(self):
        with patch("src.consumers.architect.api_client") as api:
            api.get_tasks_by_story = AsyncMock()
            api.create_task = AsyncMock(return_value={"id": "task-ci", "title": "CI check"})
            yield api

    @pytest.mark.asyncio
    async def test_appends_ci_task_blocked_by_last(self, mock_api):
        from src.consumers.architect import append_ci_check_task

        mock_api.get_tasks_by_story.return_value = [
            {"id": "task-001", "blocked_by_task_id": None, "created_by": "architect"},
            {"id": "task-002", "blocked_by_task_id": "task-001", "created_by": "architect"},
        ]

        result = await append_ci_check_task("story-abc", "proj-123")

        assert result is not None
        assert result["id"] == "task-ci"
        call_data = mock_api.create_task.call_args[0][0]
        assert call_data["blocked_by_task_id"] == "task-002"
        assert call_data["created_by"] == "system"
        assert call_data["status"] == "todo"
        assert "test" in call_data["title"].lower() or "ci" in call_data["title"].lower()

    @pytest.mark.asyncio
    async def test_skips_when_no_architect_tasks(self, mock_api):
        from src.consumers.architect import append_ci_check_task

        mock_api.get_tasks_by_story.return_value = []

        result = await append_ci_check_task("story-abc", "proj-123")

        assert result is None
        mock_api.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_only_system_tasks(self, mock_api):
        from src.consumers.architect import append_ci_check_task

        mock_api.get_tasks_by_story.return_value = [
            {"id": "task-old", "blocked_by_task_id": None, "created_by": "system"},
        ]

        result = await append_ci_check_task("story-abc", "proj-123")

        assert result is None
        mock_api.create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_finds_chain_tail(self, mock_api):
        """CI task should be blocked by the tail of the dependency chain."""
        from src.consumers.architect import append_ci_check_task

        mock_api.get_tasks_by_story.return_value = [
            {"id": "task-a", "blocked_by_task_id": None, "created_by": "architect"},
            {"id": "task-b", "blocked_by_task_id": "task-a", "created_by": "architect"},
            {"id": "task-c", "blocked_by_task_id": "task-b", "created_by": "architect"},
        ]

        await append_ci_check_task("story-abc", "proj-123")

        call_data = mock_api.create_task.call_args[0][0]
        assert call_data["blocked_by_task_id"] == "task-c"
