"""LangGraph graph definition.

After CLI Agent Migration (Phase 8), this graph contains only:
- Provisioner (standalone server provisioning via pub/sub trigger)

Engineering and Deploy flows are handled by dedicated workers:
- engineering-worker: consumes engineering:queue, runs Engineering subgraph
- deploy-worker: consumes deploy:queue, runs DevOps subgraph

User messages are handled by worker-manager + Claude CLI, not this graph.
"""

from typing import Annotated

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from .nodes import provisioner


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
    """State for orchestrator subgraphs.

    After CLI Agent migration, this state is used for:
    - Engineering subgraph (code generation pipeline)
    - DevOps subgraph (deployment pipeline)
    - Provisioner (server setup)

    For nested dict structures, see src/schemas/ for detailed schemas.
    """

    # ============================================================
    # MESSAGES (LangChain conversation history for subgraphs)
    # ============================================================
    messages: Annotated[list, add_messages]

    # ============================================================
    # PROJECT CONTEXT
    # ============================================================
    current_project: str | None
    project_spec: dict | None
    po_intent: str | None  # "new_project", "deploy" - set by orchestrator-cli

    # ============================================================
    # ALLOCATED RESOURCES
    # ============================================================
    allocated_resources: dict

    # ============================================================
    # REPOSITORY INFO
    # ============================================================
    repo_info: dict | None
    project_complexity: str | None
    architect_complete: bool

    # ============================================================
    # PREPARER STATE
    # ============================================================
    selected_modules: list[str] | None
    deployment_hints: dict | None
    custom_task_instructions: str | None
    repo_prepared: bool
    preparer_commit_sha: str | None

    # ============================================================
    # ENGINEERING SUBGRAPH STATE
    # ============================================================
    engineering_status: str
    review_feedback: str | None
    engineering_iterations: int
    test_results: dict | None

    # ============================================================
    # DEVOPS SUBGRAPH STATE
    # ============================================================
    provided_secrets: dict
    missing_user_secrets: list[str]
    deployment_result: dict | None

    # ============================================================
    # HUMAN-IN-THE-LOOP FLAGS
    # ============================================================
    needs_human_approval: bool
    human_approval_reason: str | None

    # ============================================================
    # PROVISIONING
    # ============================================================
    server_to_provision: str | None
    is_incident_recovery: bool
    provisioning_result: dict | None

    # ============================================================
    # USER CONTEXT
    # ============================================================
    telegram_user_id: int | None
    user_id: int | None

    # ============================================================
    # STATUS & RESULTS
    # ============================================================
    current_agent: Annotated[str, _last_value]
    errors: Annotated[list[str], _merge_errors]
    deployed_url: str | None


def create_graph() -> StateGraph:
    """Create the orchestrator graph.

    Currently only used for provisioner triggers (server provisioning via pub/sub).
    Engineering and Deploy flows are handled by dedicated workers with their own subgraphs.
    """
    graph = StateGraph(OrchestratorState)

    graph.add_node("provisioner", provisioner.run)

    graph.add_edge(START, "provisioner")
    graph.add_edge("provisioner", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)
