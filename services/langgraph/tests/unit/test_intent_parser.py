"""Unit tests for Intent Parser."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.nodes.intent_parser import (
    _format_capabilities_for_prompt,
    _parse_llm_response,
    run,
)


class TestFormatCapabilitiesForPrompt:
    """Tests for _format_capabilities_for_prompt."""

    def test_formats_capabilities(self):
        """Test capability formatting for prompt."""
        result = _format_capabilities_for_prompt()

        assert "deploy" in result
        assert "infrastructure" in result
        assert "project_management" in result


class TestParseLLMResponse:
    """Tests for _parse_llm_response."""

    def test_parses_valid_json(self):
        """Test parsing valid JSON."""
        content = '{"capabilities": ["deploy"], "task_summary": "Deploy", "reasoning": "..."}'
        result = _parse_llm_response(content)

        assert result["capabilities"] == ["deploy"]
        assert result["task_summary"] == "Deploy"

    def test_handles_markdown_code_block(self):
        """Test parsing JSON in markdown code block."""
        content = """```json
{"capabilities": ["infrastructure"], "task_summary": "Check servers", "reasoning": "..."}
```"""
        result = _parse_llm_response(content)

        assert result["capabilities"] == ["infrastructure"]
        assert result["task_summary"] == "Check servers"

    def test_fallback_on_invalid_json(self):
        """Test fallback when JSON is invalid."""
        content = "not valid json"
        result = _parse_llm_response(content)

        # Should return safe default
        assert result["capabilities"] == ["project_management"]
        assert "reasoning" in result


class TestIntentParserRun:
    """Tests for intent parser run function."""

    @pytest.mark.asyncio
    async def test_returns_capabilities_and_summary(self):
        """Test that run returns expected fields."""
        mock_llm = MagicMock()
        mock_response = (
            '{"capabilities": ["deploy"], "task_summary": "Deploy project", '
            '"reasoning": "User wants deploy"}'
        )
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content=mock_response))

        with patch("src.nodes.intent_parser.LLMFactory.create_llm", return_value=mock_llm):
            with patch("src.nodes.intent_parser._get_recent_messages", return_value=[]):
                with patch(
                    "src.nodes.intent_parser.generate_thread_id",
                    return_value="user_123_1",
                ):
                    from langchain_core.messages import HumanMessage

                    state = {
                        "messages": [HumanMessage(content="Deploy my project")],
                        "telegram_user_id": 123,
                    }

                    result = await run(state)

        assert result["active_capabilities"] == ["deploy"]
        assert result["task_summary"] == "Deploy project"
        assert result["thread_id"] == "user_123_1"
        assert result["current_agent"] == "intent_parser"

    @pytest.mark.asyncio
    async def test_handles_no_user_message(self):
        """Test handling when no user message is found."""
        from langchain_core.messages import AIMessage

        state = {
            "messages": [AIMessage(content="Hello")],
            "telegram_user_id": 123,
        }

        result = await run(state)

        # Should return default capabilities
        assert result["active_capabilities"] == ["project_management"]
        assert result["task_summary"] == "Unknown request"
