"""LangGraph graph definition."""

from typing import Annotated

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from .nodes import architect, brainstorm, zavhoz


class OrchestratorState(TypedDict):
    """Global state for the orchestrator."""

    # Messages (conversation history)
    messages: Annotated[list, add_messages]

    # Current project
    current_project: str | None
    project_spec: dict | None

    # Resources (handle -> resource_id mapping)
    allocated_resources: dict

    # Repository info (after architect creates it)
    repo_info: dict | None
    architect_complete: bool

    # Status
    current_agent: str
    errors: list[str]

    # Results
    deployed_url: str | None


def route_after_brainstorm(state: OrchestratorState) -> str:
    """Decide where to go after brainstorm.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If project was created -> proceed to Zavhoz
    - Otherwise -> END (waiting for user input)
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools (e.g., create_project)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "brainstorm_tools"

    # If project was created, proceed to resource allocation
    if state.get("current_project"):
        return "zavhoz"

    # Otherwise END - LLM responded with a question, wait for user
    return END


def route_after_zavhoz(state: OrchestratorState) -> str:
    """Decide where to go after zavhoz.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If resources allocated -> proceed to Architect
    - Otherwise -> END
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools (e.g., find_suitable_server, allocate_port)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "zavhoz_tools"

    # Check if resources are allocated (has at least one resource)
    allocated = state.get("allocated_resources", {})
    if allocated:
        return "architect"

    # Otherwise END - Zavhoz finished or needs input
    return END


def route_after_architect(state: OrchestratorState) -> str:
    """Decide where to go after architect.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If repo created (has repo_info) -> spawn factory worker
    - Otherwise -> END
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "architect_tools"

    # If repo was created, spawn the factory worker
    if state.get("repo_info"):
        return "architect_spawn_worker"

    # Otherwise END
    return END


def route_after_architect_tools(state: OrchestratorState) -> str:
    """Decide where to go after architect tools execution.
    
    If we have repo_info now, we can spawn the worker.
    Otherwise go back to architect for more tool calls.
    """
    if state.get("repo_info"):
        return "architect_spawn_worker"
    return "architect"


def create_graph() -> StateGraph:
    """Create the orchestrator graph."""
    graph = StateGraph(OrchestratorState)

    # Add nodes
    graph.add_node("brainstorm", brainstorm.run)
    graph.add_node("brainstorm_tools", brainstorm.execute_tools)
    graph.add_node("zavhoz", zavhoz.run)
    graph.add_node("zavhoz_tools", zavhoz.execute_tools)
    graph.add_node("architect", architect.run)
    graph.add_node("architect_tools", architect.execute_tools)
    graph.add_node("architect_spawn_worker", architect.spawn_factory_worker)

    # Add edges
    graph.add_edge(START, "brainstorm")

    # After brainstorm: either execute tools, go to zavhoz, or end
    graph.add_conditional_edges(
        "brainstorm",
        route_after_brainstorm,
        {
            "brainstorm_tools": "brainstorm_tools",
            "zavhoz": "zavhoz",
            END: END,
        },
    )

    # After brainstorm tools execution: back to brainstorm to process result
    graph.add_edge("brainstorm_tools", "brainstorm")

    # After zavhoz: either execute tools, go to architect, or end
    graph.add_conditional_edges(
        "zavhoz",
        route_after_zavhoz,
        {
            "zavhoz_tools": "zavhoz_tools",
            "architect": "architect",
            END: END,
        },
    )

    # After zavhoz tools execution: back to zavhoz to process result
    graph.add_edge("zavhoz_tools", "zavhoz")

    # After architect: either execute tools, spawn worker, or end
    graph.add_conditional_edges(
        "architect",
        route_after_architect,
        {
            "architect_tools": "architect_tools",
            "architect_spawn_worker": "architect_spawn_worker",
            END: END,
        },
    )

    # After architect tools: either spawn worker or back to architect
    graph.add_conditional_edges(
        "architect_tools",
        route_after_architect_tools,
        {
            "architect_spawn_worker": "architect_spawn_worker",
            "architect": "architect",
        },
    )

    # After spawning worker: END (for now, later will go to Developer)
    graph.add_edge("architect_spawn_worker", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)

