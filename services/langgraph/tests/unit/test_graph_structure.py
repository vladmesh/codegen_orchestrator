"""Unit tests for LangGraph structure and routing."""

import pytest

from src.graph import (
    OrchestratorState,
    create_graph,
    route_after_engineering,
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


def test_route_after_po_tools_deploy_intent():
    """Test routing to zavhoz when deploy intent is set."""
    from src.graph import route_after_product_owner_tools

    state = {"po_intent": "deploy", "current_project": "test-123"}
    assert route_after_product_owner_tools(state) == "zavhoz"
