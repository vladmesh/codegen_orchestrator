"""LangGraph graph definition.

After CLI Agent Migration (Phase 8), this graph contains only:
- Engineering Subgraph (triggered via Redis queue)
- DevOps Subgraph (triggered via Redis queue)
- Provisioner (standalone server provisioning)

User messages are handled by workers-spawner + Claude CLI, not this graph.
"""

from typing import Annotated

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from .nodes import analyst, provisioner, zavhoz
from .subgraphs.devops import create_devops_subgraph
from .subgraphs.engineering import create_engineering_subgraph


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


def route_after_zavhoz(state: OrchestratorState) -> str:
    """Decide where to go after zavhoz.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If resources allocated -> Engineering (for new_project) or DevOps (for deploy)
    - Otherwise -> END
    """
    import structlog

    logger = structlog.get_logger()

    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "zavhoz_tools"

    allocated = state.get("allocated_resources", {})
    po_intent = state.get("po_intent")

    logger.info(
        "route_after_zavhoz",
        po_intent=po_intent,
        allocated_resources_count=len(allocated),
        has_resources=bool(allocated),
    )

    if allocated:
        if po_intent == "deploy":
            return "devops"
        return "engineering"

    logger.warning(
        "zavhoz_no_resources_allocated",
        po_intent=po_intent,
        hint="Zavhoz did not allocate resources.",
    )
    return END


def route_after_engineering(state: OrchestratorState) -> str:
    """Decide where to go after engineering subgraph."""
    engineering_status = state.get("engineering_status", "idle")

    if state.get("needs_human_approval"):
        return END

    if engineering_status == "done":
        allocated_resources = state.get("allocated_resources", {})
        if allocated_resources:
            return "devops"
        return END

    return END


def route_after_analyst(state: OrchestratorState) -> str:
    """Decide where to go after analyst."""
    import structlog

    logger = structlog.get_logger()

    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "analyst_tools"

    if state.get("current_project"):
        project_spec = state.get("project_spec")
        logger.info(
            "route_after_analyst_to_zavhoz",
            current_project=state.get("current_project"),
            project_spec_exists=project_spec is not None,
        )
        return "zavhoz"

    return END


async def run_engineering_subgraph(state: OrchestratorState) -> dict:
    """Run the engineering subgraph as a single unit."""
    import structlog

    logger = structlog.get_logger()

    project_spec = state.get("project_spec")
    logger.info(
        "engineering_subgraph_starting",
        current_project=state.get("current_project"),
        project_spec_exists=project_spec is not None,
    )

    engineering_graph = create_engineering_subgraph()

    subgraph_input = {
        "messages": state.get("messages", []),
        "current_project": state.get("current_project"),
        "project_spec": state.get("project_spec"),
        "allocated_resources": state.get("allocated_resources", {}),
        "repo_info": state.get("repo_info"),
        "project_complexity": state.get("project_complexity"),
        "architect_complete": state.get("architect_complete", False),
        "selected_modules": state.get("selected_modules"),
        "deployment_hints": state.get("deployment_hints"),
        "custom_task_instructions": state.get("custom_task_instructions"),
        "repo_prepared": state.get("repo_prepared", False),
        "preparer_commit_sha": state.get("preparer_commit_sha"),
        "engineering_status": "working",
        "review_feedback": None,
        "iteration_count": 0,
        "test_results": None,
        "needs_human_approval": False,
        "human_approval_reason": None,
        "errors": state.get("errors", []),
    }

    result = await engineering_graph.ainvoke(subgraph_input)

    return {
        "messages": result.get("messages", []),
        "repo_info": result.get("repo_info"),
        "project_complexity": result.get("project_complexity"),
        "architect_complete": result.get("architect_complete", False),
        "selected_modules": result.get("selected_modules"),
        "deployment_hints": result.get("deployment_hints"),
        "custom_task_instructions": result.get("custom_task_instructions"),
        "repo_prepared": result.get("repo_prepared", False),
        "preparer_commit_sha": result.get("preparer_commit_sha"),
        "engineering_status": result.get("engineering_status", "idle"),
        "review_feedback": result.get("review_feedback"),
        "engineering_iterations": result.get("iteration_count", 0),
        "test_results": result.get("test_results"),
        "needs_human_approval": result.get("needs_human_approval", False),
        "human_approval_reason": result.get("human_approval_reason"),
        "errors": result.get("errors", []),
        "current_agent": "engineering",
    }


async def run_devops_subgraph(state: OrchestratorState) -> dict:
    """Run the DevOps subgraph as a single unit."""
    import structlog

    logger = structlog.get_logger()

    logger.info(
        "devops_subgraph_starting",
        current_project=state.get("current_project"),
        provided_secrets_count=len(state.get("provided_secrets", {})),
    )

    devops_graph = create_devops_subgraph()

    subgraph_input = {
        "messages": state.get("messages", []),
        "project_id": state.get("current_project"),
        "project_spec": state.get("project_spec"),
        "allocated_resources": state.get("allocated_resources", {}),
        "repo_info": state.get("repo_info"),
        "provided_secrets": state.get("provided_secrets", {}),
        "env_variables": [],
        "env_analysis": {},
        "resolved_secrets": {},
        "missing_user_secrets": [],
        "deployment_result": None,
        "deployed_url": None,
        "errors": state.get("errors", []),
    }

    result = await devops_graph.ainvoke(subgraph_input)

    logger.info(
        "devops_subgraph_complete",
        missing_user_secrets=result.get("missing_user_secrets", []),
        deployed_url=result.get("deployed_url"),
        has_errors=bool(result.get("errors")),
    )

    return {
        "messages": result.get("messages", []),
        "missing_user_secrets": result.get("missing_user_secrets", []),
        "deployment_result": result.get("deployment_result"),
        "deployed_url": result.get("deployed_url"),
        "errors": result.get("errors", []),
        "current_agent": "devops",
    }


def create_graph() -> StateGraph:
    """Create the orchestrator graph.

    Topology (Post CLI Agent Migration):
        START -> provisioner -> END (standalone provisioning)
        START -> analyst -> zavhoz -> engineering -> devops -> END
        START -> zavhoz -> engineering/devops -> END (queue-triggered)
    """
    graph = StateGraph(OrchestratorState)

    # Add nodes (subgraphs and supporting nodes only)
    graph.add_node("zavhoz", zavhoz.run)
    graph.add_node("zavhoz_tools", zavhoz.execute_tools)
    graph.add_node("engineering", run_engineering_subgraph)
    graph.add_node("devops", run_devops_subgraph)
    graph.add_node("provisioner", provisioner.run)
    graph.add_node("analyst", analyst.run)
    graph.add_node("analyst_tools", analyst.execute_tools)

    # Start routing
    def route_start(state: OrchestratorState) -> str:
        """Route from start based on trigger type."""
        if state.get("server_to_provision"):
            return "provisioner"
        # If triggered with project_spec, start with Zavhoz for resource allocation
        if state.get("project_spec"):
            return "zavhoz"
        # If triggered with current_project for deploy
        if state.get("current_project") and state.get("po_intent") == "deploy":
            return "zavhoz"
        # Default: analyst for project creation
        return "analyst"

    graph.add_conditional_edges(
        START,
        route_start,
        {
            "provisioner": "provisioner",
            "zavhoz": "zavhoz",
            "analyst": "analyst",
        },
    )

    # Analyst -> zavhoz or END
    graph.add_conditional_edges(
        "analyst",
        route_after_analyst,
        {
            "analyst_tools": "analyst_tools",
            "zavhoz": "zavhoz",
            END: END,
        },
    )
    graph.add_edge("analyst_tools", "analyst")

    # Zavhoz -> engineering/devops or END
    graph.add_conditional_edges(
        "zavhoz",
        route_after_zavhoz,
        {
            "zavhoz_tools": "zavhoz_tools",
            "engineering": "engineering",
            "devops": "devops",
            END: END,
        },
    )
    graph.add_edge("zavhoz_tools", "zavhoz")

    # Engineering -> devops or END
    graph.add_conditional_edges(
        "engineering",
        route_after_engineering,
        {
            "devops": "devops",
            END: END,
        },
    )

    # DevOps -> END
    graph.add_edge("devops", END)

    # Provisioner -> END
    graph.add_edge("provisioner", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)
