"""Provisioner graph definition."""

from typing import Annotated

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from .nodes.provisioner_proxy import provisioner_proxy_node


def _last_value(left: str, right: str) -> str:
    """Reducer that keeps the last (rightmost) value for concurrent updates."""
    return right


def _merge_errors(left: list[str], right: list[str]) -> list[str]:
    """Reducer that merges error lists without duplicates."""
    seen = set(left)
    result = list(left)
    for err in right:
        if err not in seen:
            result.append(err)
            seen.add(err)
    return result


class OrchestratorState(TypedDict):
    """State used by the provisioner trigger graph."""

    messages: Annotated[list, add_messages]
    server_to_provision: str | None
    is_incident_recovery: bool
    correlation_id: str | None
    provisioning_result: dict | None
    current_agent: Annotated[str, _last_value]
    errors: Annotated[list[str], _merge_errors]


def create_graph() -> StateGraph:
    """Create the orchestrator graph.

    Currently only used for provisioner triggers (server provisioning via pub/sub).
    Engineering and Deploy flows are handled by dedicated workers with their own subgraphs.
    """
    graph = StateGraph(OrchestratorState)

    graph.add_node("provisioner", provisioner_proxy_node.run)

    graph.add_edge(START, "provisioner")
    graph.add_edge("provisioner", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)
