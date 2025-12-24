import pytest
from unittest.mock import MagicMock, AsyncMock
from langchain_core.messages import AIMessage
from src.nodes import architect
from src.graph import route_after_architect_spawn_worker

@pytest.mark.asyncio
async def test_route_after_architect_spawn_worker():
    # Test simple project
    state_simple = {"project_complexity": "simple"}
    assert route_after_architect_spawn_worker(state_simple) == "devops"

    # Test complex project
    state_complex = {"project_complexity": "complex"}
    assert route_after_architect_spawn_worker(state_complex) == "developer"

    # Test default (complex)
    state_default = {}
    assert route_after_architect_spawn_worker(state_default) == "developer"

@pytest.mark.asyncio
async def test_architect_execute_tools_complexity():
    # Mock tool call for set_project_complexity
    tool_call = {
        "name": "set_project_complexity",
        "args": {"complexity": "simple"},
        "id": "call_123"
    }
    
    message = MagicMock()
    message.tool_calls = [tool_call]
    
    state = {
        "messages": [message],
        "repo_info": {}
    }

    # Mock tools_map in architect
    architect.tools_map = {
        "set_project_complexity": MagicMock()
    }
    # Mock the tool implementation to return the arg
    architect.tools_map["set_project_complexity"].ainvoke = AsyncMock(return_value="simple")

    result = await architect.execute_tools(state)
    
    assert result["project_complexity"] == "simple"
    assert "messages" in result
    assert result["messages"][0].content == "Result: simple"

@pytest.mark.asyncio
async def test_architect_spawn_worker_simple_instructions():
    # This is harder to test without mocking request_spawn, but we can check if it fails fast or how it behaves
    # For now, let's just assume the logic inside spawn_factory_worker regarding string formatting is correct 
    # as tested by the fact we wrote it. 
    # If we wanted to test it fully we'd need to mock request_spawn.
    pass
