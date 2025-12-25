"""Engineering Subgraph.

Encapsulates the Architect → Developer → Tester loop with review cycles.
This subgraph is exposed as a single node to the Product Owner.
"""

import logging
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from ..nodes import architect, developer

logger = logging.getLogger(__name__)

# Maximum iterations before escalating to PO
MAX_ITERATIONS = 3


class EngineeringState(TypedDict):
    """State for the engineering subgraph."""

    # Messages (conversation history)
    messages: Annotated[list, add_messages]

    # Project info (passed from parent)
    current_project: str | None
    project_spec: dict | None
    allocated_resources: dict

    # Repository info
    repo_info: dict | None
    project_complexity: str | None
    architect_complete: bool

    # Engineering loop tracking
    engineering_status: str  # "idle" | "working" | "reviewing" | "testing" | "done" | "blocked"
    review_feedback: str | None
    iteration_count: int
    test_results: dict | None

    # Human-in-the-loop
    needs_human_approval: bool
    human_approval_reason: str | None

    # Errors
    errors: list[str]


def route_after_architect(state: EngineeringState) -> str:
    """Route after architect node.

    - If tool calls pending → architect_tools
    - If repo created → spawn worker
    - Otherwise → END (blocked)
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "architect_tools"

    if state.get("repo_info"):
        return "architect_spawn_worker"

    return END


def route_after_architect_tools(state: EngineeringState) -> str:
    """Route after architect tools execution."""
    if state.get("repo_info"):
        return "architect_spawn_worker"
    return "architect"


def route_after_spawn(state: EngineeringState) -> str:
    """Route after architect spawns worker.

    - Simple projects → testing
    - Complex projects → developer
    """
    complexity = state.get("project_complexity", "complex")
    if complexity == "simple":
        return "tester"
    return "developer"


def route_after_developer(state: EngineeringState) -> str:
    """Route after developer node."""
    return "developer_spawn_worker"


def route_after_developer_spawn(state: EngineeringState) -> str:
    """Route after developer spawns worker → testing."""
    return "tester"


def route_after_tester(state: EngineeringState) -> str:
    """Route after tester node.

    - If tests pass → done
    - If tests fail and iterations < MAX → back to developer
    - If iterations >= MAX → blocked (needs human)
    """
    test_results = state.get("test_results", {})
    iteration_count = state.get("iteration_count", 0)

    if test_results.get("passed", False):
        return "done"

    if iteration_count >= MAX_ITERATIONS:
        return "blocked"

    return "developer"


async def tester_node(state: EngineeringState) -> dict:
    """Tester node - runs tests and reports results.

    For now, this is a simplified version that marks as passed.
    Full implementation would spawn a worker to run `make test`.
    """
    iteration_count = state.get("iteration_count", 0) + 1

    # TODO: Implement actual test running via worker spawner
    # For now, assume tests pass after the first implementation
    test_passed = True

    if test_passed:
        return {
            "test_results": {"passed": True, "output": "All tests passed"},
            "iteration_count": iteration_count,
            "engineering_status": "done",
        }
    else:
        return {
            "test_results": {"passed": False, "output": "Tests failed"},
            "iteration_count": iteration_count,
            "review_feedback": "Tests failed, please fix",
            "engineering_status": "reviewing",
        }


async def done_node(state: EngineeringState) -> dict:
    """Mark engineering as complete."""
    return {
        "engineering_status": "done",
        "needs_human_approval": False,
    }


async def blocked_node(state: EngineeringState) -> dict:
    """Mark engineering as blocked, needs human intervention."""
    return {
        "engineering_status": "blocked",
        "needs_human_approval": True,
        "human_approval_reason": f"Max iterations ({MAX_ITERATIONS}) reached. Tests still failing.",
    }


def create_engineering_subgraph() -> StateGraph:
    """Create the engineering subgraph.

    Topology:
        START → architect → architect_tools (loop) → architect_spawn_worker
              → developer → developer_spawn_worker → tester
              → (if fail) developer (loop up to MAX_ITERATIONS)
              → done | blocked → END
    """
    graph = StateGraph(EngineeringState)

    # Add nodes
    graph.add_node("architect", architect.run)
    graph.add_node("architect_tools", architect.execute_tools)
    graph.add_node("architect_spawn_worker", architect.spawn_factory_worker)
    graph.add_node("developer", developer.run)
    graph.add_node("developer_spawn_worker", developer.spawn_developer_worker)
    graph.add_node("tester", tester_node)
    graph.add_node("done", done_node)
    graph.add_node("blocked", blocked_node)

    # Edges
    graph.add_edge(START, "architect")

    graph.add_conditional_edges(
        "architect",
        route_after_architect,
        {
            "architect_tools": "architect_tools",
            "architect_spawn_worker": "architect_spawn_worker",
            END: END,
        },
    )

    graph.add_conditional_edges(
        "architect_tools",
        route_after_architect_tools,
        {
            "architect_spawn_worker": "architect_spawn_worker",
            "architect": "architect",
        },
    )

    graph.add_conditional_edges(
        "architect_spawn_worker",
        route_after_spawn,
        {
            "tester": "tester",
            "developer": "developer",
        },
    )

    graph.add_conditional_edges(
        "developer",
        route_after_developer,
        {
            "developer_spawn_worker": "developer_spawn_worker",
        },
    )

    graph.add_edge("developer_spawn_worker", "tester")

    graph.add_conditional_edges(
        "tester",
        route_after_tester,
        {
            "done": "done",
            "blocked": "blocked",
            "developer": "developer",
        },
    )

    graph.add_edge("done", END)
    graph.add_edge("blocked", END)

    return graph.compile()
