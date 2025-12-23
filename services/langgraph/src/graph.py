"""LangGraph graph definition."""

from typing import Annotated

from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from .nodes import brainstorm, zavhoz


class OrchestratorState(TypedDict):
    """Global state for the orchestrator."""

    # Messages (conversation history)
    messages: Annotated[list, add_messages]

    # Current project
    current_project: str | None
    project_spec: dict | None

    # Resources (handle -> resource_id mapping)
    allocated_resources: dict

    # Status
    current_agent: str
    errors: list[str]

    # Results
    deployed_url: str | None


def route_after_brainstorm(state: OrchestratorState) -> str:
    """Decide where to go after brainstorm."""
    if state.get("errors"):
        return END
    if state.get("project_spec"):
        return "zavhoz"
    return END


def create_graph() -> StateGraph:
    """Create the orchestrator graph."""
    graph = StateGraph(OrchestratorState)

    # Add nodes
    graph.add_node("brainstorm", brainstorm.run)
    graph.add_node("zavhoz", zavhoz.run)

    # Add edges
    graph.set_entry_point("brainstorm")
    graph.add_conditional_edges("brainstorm", route_after_brainstorm)
    graph.add_edge("zavhoz", END)

    return graph.compile()
