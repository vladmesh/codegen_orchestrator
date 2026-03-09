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
