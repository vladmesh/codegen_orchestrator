"""Simplified Engineering Subgraph.

Unified Developer node handles architecture, scaffolding, and coding.
Tester is a stub that always passes (for now).

Flow:
    START → Developer ─┬─ (error) → Blocked → END
                       └─ (ok) → Tester → Done/Blocked → END
"""

from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from ..nodes.base import FunctionalNode
from ..nodes.developer import developer_node


def _merge_errors(left: list[str], right: list[str]) -> list[str]:
    """Reducer that merges error lists without duplicates."""
    seen = set(left)
    result = list(left)
    for err in right:
        if err not in seen:
            result.append(err)
            seen.add(err)
    return result


# Maximum iterations before escalating to PO
MAX_ITERATIONS = 3


class EngineeringState(TypedDict):
    """Simplified state for the engineering subgraph."""

    # Messages (conversation history)
    messages: Annotated[list, add_messages]

    # Project info (passed from parent)
    current_project: str | None
    project_spec: dict | None
    allocated_resources: dict

    # Engineering result
    engineering_status: str  # "idle" | "working" | "done" | "blocked"
    commit_sha: str | None

    # Loop tracking
    iteration_count: int
    test_results: dict | None

    # Human-in-the-loop
    needs_human_approval: bool
    human_approval_reason: str | None

    # Errors (merges without duplicates)
    errors: Annotated[list[str], _merge_errors]


def route_after_developer(state: EngineeringState) -> str:
    """Route after developer node.

    If developer returned 'blocked' (error/exception), skip tester and go to blocked.
    Otherwise proceed to tester for validation.
    """
    status = state.get("engineering_status", "idle")

    if status == "blocked":
        return "blocked"

    # Also check for errors list
    errors = state.get("errors", [])
    if errors:
        return "blocked"

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


class TesterNode(FunctionalNode):
    """Tester node - stub that always passes for MVP."""

    def __init__(self):
        super().__init__(node_id="tester")

    async def run(self, state: EngineeringState) -> dict:
        """Run tests and update engineering state."""
        import structlog

        logger = structlog.get_logger()
        logger.info("tester_node_run_start", iteration_count=state.get("iteration_count", 0))

        iteration_count = state.get("iteration_count", 0) + 1

        # Stub: always pass for MVP
        # Real testing tracked in docs/backlog.md
        test_passed = True

        if test_passed:
            return {
                "test_results": {"passed": True, "output": "All tests passed"},
                "iteration_count": iteration_count,
                "engineering_status": "done",
            }
        return {
            "test_results": {"passed": False, "output": "Tests failed"},
            "iteration_count": iteration_count,
            "engineering_status": "reviewing",
        }


class DoneNode(FunctionalNode):
    """Mark engineering as complete."""

    def __init__(self):
        super().__init__(node_id="done")

    async def run(self, state: EngineeringState) -> dict:
        return {
            "engineering_status": "done",
            "needs_human_approval": False,
        }


class BlockedNode(FunctionalNode):
    """Mark engineering as blocked, needs human intervention."""

    def __init__(self):
        super().__init__(node_id="blocked")

    async def run(self, state: EngineeringState) -> dict:
        return {
            "engineering_status": "blocked",
            "needs_human_approval": True,
            "human_approval_reason": (
                f"Max iterations ({MAX_ITERATIONS}) reached. Tests still failing."
            ),
        }


tester_node = TesterNode()
done_node = DoneNode()
blocked_node = BlockedNode()


def create_engineering_subgraph() -> StateGraph:
    """Create the simplified engineering subgraph.

    Topology:
        START → developer ─┬─ (blocked) → blocked → END
                           └─ (ok) → tester ─┬─ (pass) → done → END
                                             ├─ (fail, retries left) → developer
                                             └─ (fail, max retries) → blocked → END

    Changes from old architecture:
    - Removed architect, architect_tools, preparer nodes
    - Developer is now unified (architecture + scaffolding + coding)
    - Tester is a stub (always passes)
    - Developer errors skip tester and go directly to blocked
    """
    graph = StateGraph(EngineeringState)

    # Add nodes
    graph.add_node("developer", developer_node.run)
    graph.add_node("tester", tester_node.run)
    graph.add_node("done", done_node.run)
    graph.add_node("blocked", blocked_node.run)

    # Edges
    graph.add_edge(START, "developer")

    # Developer → tester OR blocked (if developer failed)
    graph.add_conditional_edges(
        "developer",
        route_after_developer,
        {
            "tester": "tester",
            "blocked": "blocked",
        },
    )

    # Tester → done, blocked, or back to developer
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
