"""Tests for architect and preparer routing in the engineering subgraph."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.nodes import architect
from src.subgraphs.engineering import route_after_preparer


@pytest.mark.asyncio
async def test_route_after_preparer():
    """Test routing after preparer node."""
    # Test simple project -> tester
    state_simple = {"repo_prepared": True, "project_complexity": "simple"}
    assert route_after_preparer(state_simple) == "tester"

    # Test complex project -> developer
    state_complex = {"repo_prepared": True, "project_complexity": "complex"}
    assert route_after_preparer(state_complex) == "developer"

    # Test default (complex) when no complexity set
    state_default = {"repo_prepared": True}
    assert route_after_preparer(state_default) == "developer"

    # Test preparation failed -> END
    from langgraph.graph import END

    state_failed = {"repo_prepared": False}
    assert route_after_preparer(state_failed) == END


@pytest.mark.asyncio
async def test_architect_execute_tools_complexity():
    """Test architect tool execution for complexity setting."""
    # Mock tool call for set_project_complexity
    tool_call = {
        "name": "set_project_complexity",
        "args": {"complexity": "simple"},
        "id": "call_123",
    }

    message = MagicMock()
    message.tool_calls = [tool_call]

    state = {"messages": [message], "repo_info": {}}

    # Mock tools_map in architect
    architect.tools_map = {"set_project_complexity": MagicMock()}
    # Mock the tool implementation to return the expected result
    architect.tools_map["set_project_complexity"].ainvoke = AsyncMock(
        return_value="Project complexity set to: simple"
    )

    result = await architect.execute_tools(state)

    assert result["project_complexity"] == "simple"
    assert "messages" in result


@pytest.mark.asyncio
async def test_architect_execute_tools_modules():
    """Test architect tool execution for module selection."""
    # Mock tool call for select_modules
    tool_call = {
        "name": "select_modules",
        "args": {"modules": ["backend", "tg_bot"]},
        "id": "call_456",
    }

    message = MagicMock()
    message.tool_calls = [tool_call]

    state = {"messages": [message], "repo_info": {}}

    # Mock tools_map in architect
    architect.tools_map = {"select_modules": MagicMock()}
    # Mock the tool implementation to return the expected result
    architect.tools_map["select_modules"].ainvoke = AsyncMock(
        return_value="Selected modules: ['backend', 'tg_bot']"
    )

    result = await architect.execute_tools(state)

    assert result["selected_modules"] == ["backend", "tg_bot"]
    assert "messages" in result


@pytest.mark.asyncio
async def test_architect_execute_tools_custom_instructions():
    """Test architect tool execution for custom task instructions."""
    # Mock tool call for customize_task_instructions
    tool_call = {
        "name": "customize_task_instructions",
        "args": {"instructions": "Use Redis for caching"},
        "id": "call_789",
    }

    message = MagicMock()
    message.tool_calls = [tool_call]

    state = {"messages": [message], "repo_info": {}}

    # Mock tools_map in architect
    architect.tools_map = {"customize_task_instructions": MagicMock()}
    # Mock the tool implementation to return the expected result
    architect.tools_map["customize_task_instructions"].ainvoke = AsyncMock(
        return_value="Custom instructions saved (21 chars)"
    )

    result = await architect.execute_tools(state)

    assert result["custom_task_instructions"] == "Use Redis for caching"
    assert "messages" in result
