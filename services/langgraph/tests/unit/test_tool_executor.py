"""Unit tests for ToolExecutor."""

from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel
import pytest

from src.nodes.tool_executor import ToolExecutor


class SampleResult(BaseModel):
    """Sample Pydantic model for testing serialization."""

    status: str
    value: int


class TestToolExecutor:
    """Tests for ToolExecutor class."""

    @pytest.fixture
    def mock_tool(self):
        """Create a mock tool."""
        tool = MagicMock(spec=BaseTool)
        tool.name = "test_tool"
        tool.ainvoke = AsyncMock()
        return tool

    @pytest.fixture
    def tools_map(self, mock_tool):
        """Create tools map with mock tool."""
        return {mock_tool.name: mock_tool}

    @pytest.fixture
    def executor(self, tools_map):
        """Create ToolExecutor instance."""
        return ToolExecutor(tools_map)

    @pytest.mark.asyncio
    async def test_execute_single_tool_success(self, executor, mock_tool):
        """Test successful execution of a single tool."""
        mock_tool.ainvoke.return_value = "success result"

        tool_call = {
            "name": "test_tool",
            "args": {"param": "value"},
            "id": "call_123",
        }

        result = await executor.execute_single_tool(tool_call, {})

        assert "message" in result
        assert isinstance(result["message"], ToolMessage)
        assert "success result" in result["message"].content
        assert result["message"].tool_call_id == "call_123"
        mock_tool.ainvoke.assert_called_once_with({"param": "value"})

    @pytest.mark.asyncio
    async def test_execute_single_tool_pydantic_serialization(self, executor, mock_tool):
        """Test Pydantic model serialization."""
        mock_tool.ainvoke.return_value = SampleResult(status="ok", value=42)

        tool_call = {
            "name": "test_tool",
            "args": {},
            "id": "call_456",
        }

        result = await executor.execute_single_tool(tool_call, {})

        # Check that Pydantic model was serialized
        assert "message" in result
        content = result["message"].content
        assert "status" in content
        assert "ok" in content
        assert "42" in content

    @pytest.mark.asyncio
    async def test_execute_single_tool_unknown_tool(self, executor):
        """Test handling of unknown tool."""
        tool_call = {
            "name": "unknown_tool",
            "args": {},
            "id": "call_789",
        }

        result = await executor.execute_single_tool(tool_call, {})

        assert "message" in result
        assert "Unknown tool: unknown_tool" in result["message"].content
        assert result["message"].tool_call_id == "call_789"

    @pytest.mark.asyncio
    async def test_execute_single_tool_with_error(self, executor, mock_tool):
        """Test error handling during tool execution."""
        mock_tool.ainvoke.side_effect = ValueError("Test error")

        tool_call = {
            "name": "test_tool",
            "args": {},
            "id": "call_error",
        }

        result = await executor.execute_single_tool(tool_call, {})

        assert "message" in result
        assert "Error executing test_tool" in result["message"].content
        assert "Test error" in result["message"].content
        assert result["message"].tool_call_id == "call_error"

    @pytest.mark.asyncio
    async def test_execute_single_tool_with_result_handler(self, mock_tool):
        """Test that result_handler is called correctly."""
        result_handler = MagicMock(return_value={"custom_key": "custom_value"})
        executor = ToolExecutor({mock_tool.name: mock_tool}, result_handler)

        mock_tool.ainvoke.return_value = "result"

        tool_call = {
            "name": "test_tool",
            "args": {"arg": "val"},
            "id": "call_handler",
        }

        state = {"existing": "state"}
        result = await executor.execute_single_tool(tool_call, state)

        # Check that result_handler was called
        result_handler.assert_called_once_with("test_tool", "result", state)

        # Check that state updates were included
        assert "state_updates" in result
        assert result["state_updates"] == {"custom_key": "custom_value"}

    @pytest.mark.asyncio
    async def test_execute_tools_multiple(self, executor, mock_tool):
        """Test execution of multiple tools."""
        mock_tool.ainvoke.return_value = "result"

        tool_calls = [
            {"name": "test_tool", "args": {}, "id": "call_1"},
            {"name": "test_tool", "args": {}, "id": "call_2"},
        ]

        result = await executor.execute_tools(tool_calls, {})

        expected_message_count = 2
        assert "messages" in result
        assert len(result["messages"]) == expected_message_count
        assert all(isinstance(msg, ToolMessage) for msg in result["messages"])
        assert result["messages"][0].tool_call_id == "call_1"
        assert result["messages"][1].tool_call_id == "call_2"

    @pytest.mark.asyncio
    async def test_execute_tools_merges_state_updates(self, mock_tool):
        """Test that state updates from multiple tools are merged."""

        def side_effect_handler(tool_name, result, state):
            if result == "first":
                return {"key1": "value1"}
            elif result == "second":
                return {"key2": "value2"}
            return {}

        result_handler = MagicMock(side_effect=side_effect_handler)
        executor = ToolExecutor({mock_tool.name: mock_tool}, result_handler)

        mock_tool.ainvoke.side_effect = ["first", "second"]

        tool_calls = [
            {"name": "test_tool", "args": {}, "id": "call_1"},
            {"name": "test_tool", "args": {}, "id": "call_2"},
        ]

        result = await executor.execute_tools(tool_calls, {})

        # Check that both state updates are present
        assert result.get("key1") == "value1"
        assert result.get("key2") == "value2"

    @pytest.mark.asyncio
    async def test_execute_tools_empty_list(self, executor):
        """Test execution with empty tool calls list."""
        result = await executor.execute_tools([], {})

        assert result == {"messages": []}
