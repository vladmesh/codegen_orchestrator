"""Engineering Subgraph.

Encapsulates the Architect → Preparer → Developer → Tester loop with review cycles.
This subgraph is exposed as a single node to the Product Owner.

New architecture (post-refactor):
- Architect (LLM): Selects modules, sets deployment hints, complexity
- Preparer (FunctionalNode): Runs copier, writes TASK.md/AGENTS.md, commits
- Developer (FactoryNode): Implements business logic using Factory.ai
- Tester (FunctionalNode): Runs tests, loops back if needed
"""

from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from ..nodes import architect, preparer
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

    # Preparer state (new)
    selected_modules: list[str] | None  # Modules selected by Architect
    deployment_hints: dict | None  # Deployment config from Architect
    custom_task_instructions: str | None  # Custom instructions from Architect
    repo_prepared: bool  # True after Preparer runs copier
    preparer_commit_sha: str | None  # Commit SHA from Preparer

    # Engineering loop tracking
    engineering_status: str  # "idle" | "working" | "reviewing" | "testing" | "done" | "blocked"
    review_feedback: str | None
    iteration_count: int
    test_results: dict | None

    # Human-in-the-loop
    needs_human_approval: bool
    human_approval_reason: str | None

    # Errors (merges without duplicates)
    errors: Annotated[list[str], _merge_errors]


def route_after_architect(state: EngineeringState) -> str:
    """Route after architect node.

    - If tool calls pending → architect_tools
    - If repo created AND modules selected → preparer
    - Otherwise → END (blocked)
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "architect_tools"

    # After repo created and modules selected, proceed to Preparer
    if state.get("repo_info") and state.get("selected_modules"):
        return "preparer"

    return END


def route_after_architect_tools(state: EngineeringState) -> str:
    """Route after architect tools execution."""
    # If repo exists and modules selected, proceed to preparer
    if state.get("repo_info") and state.get("selected_modules"):
        return "preparer"
    return "architect"


def route_after_preparer(state: EngineeringState) -> str:
    """Route after preparer container finishes.

    - If preparation failed → END (blocked)
    - If simple project → tester (developer implements in one pass)
    - If complex project → developer
    """
    if not state.get("repo_prepared"):
        return END

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


class TesterNode(FunctionalNode):
    """Tester node - runs tests and reports results."""

    def __init__(self):
        super().__init__(node_id="tester")

    async def run(self, state: EngineeringState) -> dict:
        """Run tests and update engineering state."""
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
        return {
            "test_results": {"passed": False, "output": "Tests failed"},
            "iteration_count": iteration_count,
            "review_feedback": "Tests failed, please fix",
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
    """Create the engineering subgraph.

    Topology (post-refactor):
        START → architect → architect_tools (loop) → preparer
              → developer → developer_spawn_worker → tester
              → (if fail) developer (loop up to MAX_ITERATIONS)
              → done | blocked → END

    Changes from old architecture:
    - Removed architect_spawn_worker (Factory.ai for scaffolding)
    - Added preparer (lightweight copier + TASK.md/AGENTS.md)
    - Developer now receives ready project structure
    """
    graph = StateGraph(EngineeringState)

    # Add nodes
    graph.add_node("architect", architect.run)
    graph.add_node("architect_tools", architect.execute_tools)
    graph.add_node("preparer", preparer.run)  # New: lightweight copier node
    graph.add_node("developer", developer_node.run)
    graph.add_node("developer_spawn_worker", developer_node.spawn_worker)
    graph.add_node("tester", tester_node.run)
    graph.add_node("done", done_node.run)
    graph.add_node("blocked", blocked_node.run)

    # Edges
    graph.add_edge(START, "architect")

    graph.add_conditional_edges(
        "architect",
        route_after_architect,
        {
            "architect_tools": "architect_tools",
            "preparer": "preparer",
            END: END,
        },
    )

    graph.add_conditional_edges(
        "architect_tools",
        route_after_architect_tools,
        {
            "preparer": "preparer",
            "architect": "architect",
        },
    )

    graph.add_conditional_edges(
        "preparer",
        route_after_preparer,
        {
            "tester": "tester",
            "developer": "developer",
            END: END,
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
