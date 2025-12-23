"""Tests for LangGraph."""

import pytest

from services.langgraph.src.graph import OrchestratorState, create_graph


@pytest.mark.asyncio
async def test_graph_creation():
    """Test that graph can be created."""
    graph = create_graph()
    assert graph is not None


@pytest.mark.asyncio
async def test_initial_state():
    """Test initial state structure."""
    state: OrchestratorState = {
        "messages": [],
        "current_project": None,
        "project_spec": None,
        "allocated_resources": {},
        "current_agent": "",
        "errors": [],
        "deployed_url": None,
    }
    assert state["current_project"] is None
