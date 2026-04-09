"""Unit tests for architect consumer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.unit.factories import make_project, make_story

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.architect import ArchitectMessage

# Default project response (ACTIVE = scaffold done, no waiting)
_ACTIVE_PROJECT = make_project(status=ProjectStatus.ACTIVE, config={})

# Default story response (CREATED = ready for architect decomposition)
_CREATED_STORY = make_story(id="story-abc", status="created")


@pytest.fixture(autouse=True)
def _mock_api_get_project():
    """All tests get a pre-scaffolded (ACTIVE) project and CREATED story by default."""
    with patch("src.consumers.architect.api_client") as mock_api:
        mock_api.get_project = AsyncMock(return_value=_ACTIVE_PROJECT)
        mock_api.get_story = AsyncMock(return_value=_CREATED_STORY)
        # Preserve other methods as AsyncMock so tests can override
        mock_api.get_tasks_by_story = AsyncMock(return_value=[])
        mock_api.transition_story = AsyncMock()
        yield mock_api


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
    async def test_skips_deploying_story(self, mock_redis, valid_job_data, _mock_api_get_project):
        """Architect skips stories that are already deploying.

        NOTE: COMPLETED/ARCHIVED/FAILED are now caught by the centralized
        staleness guard in _base.py and never reach process_architect_job.
        """
        _mock_api_get_project.get_story = AsyncMock(
            return_value=make_story(id="story-abc", status=StoryStatus.DEPLOYING)
        )
        from src.consumers.architect import process_architect_job

        result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "skipped"
        assert StoryStatus.DEPLOYING in result["reason"]

    @pytest.mark.asyncio
    async def test_skips_when_story_not_found(
        self, mock_redis, valid_job_data, _mock_api_get_project
    ):
        """Architect skips when story no longer exists (404)."""
        _mock_api_get_project.get_story = AsyncMock(side_effect=Exception("404 Not Found"))
        from src.consumers.architect import process_architect_job

        result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "skipped"
        assert "not found" in result["error"]

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
    async def test_reopen_message_includes_user_report(self, mock_redis, _mock_api_get_project):
        """Reopen messages include user_report in the initial state."""
        _mock_api_get_project.get_story = AsyncMock(
            return_value=make_story(id="story-reopen", status=StoryStatus.REOPENED)
        )
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
        # Verify story was transitioned to in_progress after architect finished
        _mock_api_get_project.transition_story.assert_called_with("story-reopen", "start")

    @pytest.mark.asyncio
    async def test_normal_message_no_reopen_context(self, mock_redis, valid_job_data):
        """Normal messages use standard decomposition prompt."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
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

    @pytest.mark.asyncio
    async def test_waits_for_scaffold_then_proceeds(
        self, mock_redis, valid_job_data, _mock_api_get_project
    ):
        """Architect waits when project is DRAFT, proceeds when it becomes ACTIVE."""
        mock_api = _mock_api_get_project
        # First call: DRAFT, second call: ACTIVE
        mock_api.get_project = AsyncMock(
            side_effect=[
                make_project(status=ProjectStatus.DRAFT, config={}),
                make_project(status=ProjectStatus.ACTIVE, config={}),
            ]
        )

        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        assert mock_api.get_project.call_count == 2


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
    async def test_full_flow_creates_tasks(self, mock_redis, valid_job_data, _mock_api_get_project):
        """Graph creates architect tasks — no CI task appended."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="key",
                architect_llm_model="model",
                architect_llm_base_url="http://test",
            )

            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
