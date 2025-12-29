"""Unit tests for LangGraph structure and routing."""

import pytest

from src.graph import (
    OrchestratorState,
    create_graph,
    route_after_analyst,
    route_after_engineering,
    route_after_zavhoz,
)


@pytest.mark.asyncio
async def test_graph_creation():
    """Test that graph can be created."""
    graph = create_graph()
    assert graph is not None


@pytest.mark.asyncio
async def test_initial_state():
    """Test initial state structure with new Phase 3/4 fields."""
    state: OrchestratorState = {
        "messages": [],
        "current_project": None,
        "project_spec": None,
        "project_intent": None,
        "po_intent": None,
        "analyst_task": None,
        "allocated_resources": {},
        "repo_info": None,
        "project_complexity": None,
        "architect_complete": False,
        "engineering_status": "idle",
        "review_feedback": None,
        "engineering_iterations": 0,
        "test_results": None,
        "needs_human_approval": False,
        "human_approval_reason": None,
        "server_to_provision": None,
        "is_incident_recovery": False,
        "provisioning_result": None,
        "current_agent": "",
        "errors": [],
        "deployed_url": None,
    }
    assert state["current_project"] is None
    assert state["engineering_status"] == "idle"
    assert state["analyst_task"] is None


def test_route_after_engineering_done_with_resources():
    """Test routing to devops when engineering done and resources allocated."""
    state = {
        "engineering_status": "done",
        "needs_human_approval": False,
        "allocated_resources": {"server:8080": {"port": 8080}},
    }
    assert route_after_engineering(state) == "devops"


def test_route_after_engineering_done_no_resources():
    """Test routing to END when engineering done but no resources."""
    state = {
        "engineering_status": "done",
        "needs_human_approval": False,
        "allocated_resources": {},
    }
    from langgraph.graph import END

    assert route_after_engineering(state) == END


def test_route_after_engineering_blocked():
    """Test routing to END when human approval needed."""
    state = {
        "engineering_status": "blocked",
        "needs_human_approval": True,
        "allocated_resources": {"server:8080": {"port": 8080}},
    }
    from langgraph.graph import END

    assert route_after_engineering(state) == END


def test_route_after_zavhoz_to_engineering():
    """Test routing to engineering when resources allocated for new project."""

    class MockMessage:
        tool_calls = None

    # New project with allocated resources → engineering
    state = {
        "messages": [MockMessage()],
        "po_intent": "new_project",
        "allocated_resources": {"server:8080": {"port": 8080}},
    }
    assert route_after_zavhoz(state) == "engineering"


def test_route_after_zavhoz_to_devops():
    """Test routing to devops when resources allocated for deploy intent."""

    class MockMessage:
        tool_calls = None

    # Deploy intent with allocated resources → devops directly
    state = {
        "messages": [MockMessage()],
        "po_intent": "deploy",
        "allocated_resources": {"server:8080": {"port": 8080}},
    }
    assert route_after_zavhoz(state) == "devops"


def test_route_after_zavhoz_no_resources():
    """Test routing to END when no resources allocated."""
    from langgraph.graph import END

    class MockMessage:
        tool_calls = None

    state = {
        "messages": [MockMessage()],
        "po_intent": "new_project",
        "allocated_resources": {},
    }
    assert route_after_zavhoz(state) == END


def test_route_after_po_tools_deploy_intent():
    """Test routing to zavhoz when deploy intent is set."""
    from src.graph import route_after_product_owner_tools

    state = {"po_intent": "deploy", "current_project": "test-123"}
    assert route_after_product_owner_tools(state) == "zavhoz"


def test_route_after_po_tools_delegate_analyst():
    """Test routing to analyst when delegate_analyst intent is set."""
    from src.graph import route_after_product_owner_tools

    state = {"po_intent": "delegate_analyst"}
    assert route_after_product_owner_tools(state) == "analyst"


def test_route_after_analyst_with_project():
    """Test routing to zavhoz when project created by analyst.

    Sequential flow: Analyst → Zavhoz → Engineering → DevOps
    (Zavhoz allocates resources first, then Engineering builds)
    """

    class MockMessage:
        tool_calls = None

    state = {"messages": [MockMessage()], "current_project": "test-project-123"}
    result = route_after_analyst(state)
    # Should go to zavhoz for resource allocation
    assert result == "zavhoz"


def test_route_after_analyst_no_project():
    """Test routing to END when analyst is still gathering requirements."""
    from langgraph.graph import END

    class MockMessage:
        tool_calls = None

    state = {"messages": [MockMessage()], "current_project": None}
    assert route_after_analyst(state) == END


def test_route_after_analyst_with_tool_calls():
    """Test routing to analyst_tools when analyst wants to call tools."""

    class MockMessage:
        tool_calls = [{"name": "create_project", "args": {}}]

    state = {"messages": [MockMessage()], "current_project": None}
    assert route_after_analyst(state) == "analyst_tools"
