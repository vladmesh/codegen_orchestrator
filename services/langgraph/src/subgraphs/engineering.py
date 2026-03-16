"""Simplified Engineering Subgraph.

Unified Developer node handles architecture, scaffolding, and coding.

Flow:
    START → Developer ─┬─ (error) → Blocked → END
                       └─ (ok) → Done → END
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


class EngineeringState(TypedDict):
    """Simplified state for the engineering subgraph."""

    # Messages (conversation history)
    messages: Annotated[list, add_messages]

    # Project info (passed from parent)
    current_project: str | None
    project_spec: dict | None
    allocated_resources: dict

    # Task type and description
    action: str  # "create" | "feature" | "fix"
    description: str | None  # Human-readable task description

    # Story context (previous tasks + events for worker continuity)
    story_context: str | None

    # .story/STORY.md content (file-first context: goal, task list, references)
    story_md: str | None

    # Repository info (for workspace mounting)
    repo_id: str | None

    # Story branch name (e.g. "story/{story_id}")
    branch: str | None

    # Engineering result
    engineering_status: str  # "idle" | "working" | "done" | "blocked" | "worker_rejected"
    commit_sha: str | None
    worker_id: str | None
    worker_report: str | None
    reject_reason: str | None

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

    If developer returned 'blocked' or 'worker_rejected', go to blocked.
    Otherwise proceed to done.
    """
    status = state.get("engineering_status", "idle")

    if status in ("blocked", "worker_rejected"):
        return "blocked"

    # Also check for errors list
    errors = state.get("errors", [])
    if errors:
        return "blocked"

    return "done"


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
            "human_approval_reason": "Developer failed to complete the task.",
        }


done_node = DoneNode()
blocked_node = BlockedNode()


def create_engineering_subgraph() -> StateGraph:
    """Create the simplified engineering subgraph.

    Topology:
        START → developer ─┬─ (blocked) → blocked → END
                           └─ (ok) → done → END

    Tester node removed — was a stub (always passed). Future tester will be
    added after deploy (staging or prod validation). See docs/backlog.md.

    CI checks are handled by _wait_for_ci_and_fix in engineering_worker.py,
    which runs after this subgraph completes.
    """
    graph = StateGraph(EngineeringState)

    # Add nodes
    graph.add_node("developer", developer_node.run)
    graph.add_node("done", done_node.run)
    graph.add_node("blocked", blocked_node.run)

    # Edges
    graph.add_edge(START, "developer")

    # Developer → done OR blocked (if developer failed)
    graph.add_conditional_edges(
        "developer",
        route_after_developer,
        {
            "done": "done",
            "blocked": "blocked",
        },
    )

    graph.add_edge("done", END)
    graph.add_edge("blocked", END)

    return graph.compile()
