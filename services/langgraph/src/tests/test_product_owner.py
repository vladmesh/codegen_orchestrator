from unittest.mock import AsyncMock, MagicMock

import pytest

from src.nodes import product_owner


@pytest.mark.asyncio
async def test_product_owner_execute_tools_sets_intent():
    tool_call = {
        "name": "create_project_intent",
        "args": {"intent": "new_project", "summary": "Test project"},
        "id": "call_1",
    }

    message = MagicMock()
    message.tool_calls = [tool_call]

    state = {"messages": [message], "po_intent": None, "project_intent": None}

    product_owner.tools_map = {"create_project_intent": MagicMock()}
    product_owner.tools_map["create_project_intent"].ainvoke = AsyncMock(
        return_value={"intent": "new_project", "summary": "Test project", "project_id": None}
    )

    result = await product_owner.execute_tools(state)

    assert result["po_intent"] == "new_project"
    assert result["project_intent"]["summary"] == "Test project"


@pytest.mark.asyncio
async def test_product_owner_execute_tools_formats_project_list():
    tool_call = {"name": "list_projects", "args": {}, "id": "call_2"}
    message = MagicMock()
    message.tool_calls = [tool_call]

    state = {"messages": [message]}

    projects = [
        {"id": "proj-1", "name": "alpha", "status": "draft", "config": {"description": "Alpha"}},
        {"id": "proj-2", "name": "beta", "status": "active", "config": {}},
    ]

    product_owner.tools_map = {"list_projects": MagicMock()}
    product_owner.tools_map["list_projects"].ainvoke = AsyncMock(return_value=projects)

    result = await product_owner.execute_tools(state)

    assert result["messages"]
    content = result["messages"][0].content
    assert "proj-1" in content
    assert "proj-2" in content
