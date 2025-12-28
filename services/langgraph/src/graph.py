"""LangGraph graph definition."""

from typing import Annotated

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Send
from typing_extensions import TypedDict

from .nodes import analyst, devops, product_owner, provisioner, zavhoz
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
    """Global state for the orchestrator.

    This TypedDict defines all state fields passed between nodes.
    For nested dict structures, see src/schemas/ for detailed schemas:

    - repo_info: See `src.schemas.RepoInfo`
    - allocated_resources: Values are `src.schemas.AllocatedResource`
    - project_intent: See `src.schemas.ProjectIntent`
    - provisioning_result: See `src.schemas.ProvisioningResult`
    - test_results: See `src.schemas.TestResults`
    """

    # ============================================================
    # MESSAGES (LangChain conversation history)
    # ============================================================
    messages: Annotated[list, add_messages]

    # ============================================================
    # PROJECT CONTEXT
    # ============================================================
    # Project ID currently being worked on
    current_project: str | None
    # Project specification from Brainstorm (see schemas.ProjectSpec)
    project_spec: dict | None
    # Intent classification (see services.langgraph.src.schemas.ProjectIntent)
    project_intent: dict | None
    # High-level intent: "new_project", "maintenance", "deploy", or "delegate_analyst"
    po_intent: str | None
    # Task description for Analyst (set by PO when delegating)
    analyst_task: str | None

    # ============================================================
    # ALLOCATED RESOURCES
    # Keys: "server_handle:port" or service name
    # Values: AllocatedResource schema (server_handle, server_ip, port, service_name)
    # ============================================================
    allocated_resources: dict

    # ============================================================
    # REPOSITORY INFO
    # Set by Architect after creating GitHub repo
    # Schema: shared.schemas.RepoInfo (full_name, html_url, clone_url)
    # ============================================================
    repo_info: dict | None
    # Project complexity: "simple" or "complex"
    project_complexity: str | None
    # Whether Architect phase is complete
    architect_complete: bool

    # ============================================================
    # PREPARER STATE (from Architect node)
    # ============================================================
    # Modules selected by Architect (e.g., ["backend", "tg_bot"])
    selected_modules: list[str] | None
    # Deployment configuration hints from Architect
    deployment_hints: dict | None
    # Custom instructions for Developer from Architect
    custom_task_instructions: str | None
    # Whether Preparer has run copier and committed
    repo_prepared: bool
    # Commit SHA from Preparer
    preparer_commit_sha: str | None

    # ============================================================
    # ENGINEERING SUBGRAPH STATE
    # Tracks Architect → Developer → Tester loop
    # ============================================================
    # Status: "idle", "working", "done", "blocked"
    engineering_status: str
    # Feedback from code review iteration
    review_feedback: str | None
    # Number of iterations through the loop
    engineering_iterations: int
    # Test results (see shared.schemas.TestResults)
    test_results: dict | None

    # ============================================================
    # HUMAN-IN-THE-LOOP FLAGS
    # ============================================================
    # Whether human approval is needed to continue
    needs_human_approval: bool
    # Reason for requesting human approval
    human_approval_reason: str | None

    # ============================================================
    # PROVISIONING (Server setup)
    # ============================================================
    # Server handle to provision (e.g., "vps-267179")
    server_to_provision: str | None
    # If True, redeploy services after provisioning (incident recovery)
    is_incident_recovery: bool
    # Result from provisioner (see shared.schemas.ProvisioningResult)
    provisioning_result: dict | None

    # ============================================================
    # USER CONTEXT (Multi-tenancy)
    # ============================================================
    # Telegram user ID (from Telegram API, e.g., 123456789)
    telegram_user_id: int | None
    # Internal database user.id (from users table, e.g., 42)
    user_id: int | None

    # ============================================================
    # STATUS & RESULTS
    # ============================================================
    # Current active agent name (uses last-value reducer for parallel updates)
    current_agent: Annotated[str, _last_value]
    # List of accumulated errors (merges without duplicates)
    errors: Annotated[list[str], _merge_errors]
    # Deployed application URL (e.g., "http://1.2.3.4:8080")
    deployed_url: str | None


# route_after_brainstorm removed


def route_after_zavhoz(state: OrchestratorState) -> str:
    """Decide where to go after zavhoz.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If deploy intent AND resources allocated -> DevOps
    - Otherwise -> END (Engineering runs in parallel)
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools (e.g., find_suitable_server, allocate_port)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "zavhoz_tools"

    # If activation flow (deploy intent) -> proceed to DevOps ONLY if resources allocated
    if state.get("po_intent") == "deploy":
        allocated = state.get("allocated_resources", {})
        if allocated:
            return "devops"
        # Resources not allocated - Zavhoz didn't complete allocation
        # This is likely a miscommunication, log and end
        # TODO: Could loop back to zavhoz with a more explicit message
        return END

    return END


def route_after_product_owner(state: OrchestratorState) -> str:
    """Decide where to go after product owner.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If intent is new project -> proceed to Brainstorm
    - Otherwise -> END
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "product_owner_tools"

    if state.get("po_intent") == "new_project":
        return "analyst"

    return END


def route_after_product_owner_tools(state: OrchestratorState) -> str:
    """Decide where to go after product owner tools execution."""
    po_intent = state.get("po_intent")

    if po_intent == "new_project":
        return "analyst"

    if po_intent == "maintenance":
        # Project update → Engineering directly
        return "engineering"

    if po_intent == "deploy":
        # Discovered project activation → Zavhoz for resource allocation
        return "zavhoz"

    if po_intent == "delegate_analyst":
        # New project or requirements change → Analyst
        return "analyst"

    return END


def route_after_engineering(state: OrchestratorState) -> str:
    """Decide where to go after engineering subgraph.

    Routing logic:
    - If blocked (needs human) -> END (wait for user)
    - If done and resources allocated -> DevOps
    - If done but resources pending -> wait (END)
    """
    engineering_status = state.get("engineering_status", "idle")

    # Human-in-the-loop: blocked, needs human approval
    if state.get("needs_human_approval"):
        return END

    # Engineering completed successfully
    if engineering_status == "done":
        # Pre-flight check: ensure resources are allocated
        allocated_resources = state.get("allocated_resources", {})
        if allocated_resources:
            return "devops"
        # Resources not ready yet, wait
        return END

    return END


async def run_engineering_subgraph(state: OrchestratorState) -> dict:
    """Run the engineering subgraph as a single unit.

    This node invokes the compiled engineering subgraph and
    propagates its final state back.
    """
    import structlog

    logger = structlog.get_logger()

    # Debug: log what state we received
    project_spec = state.get("project_spec")
    logger.info(
        "engineering_subgraph_starting",
        current_project=state.get("current_project"),
        project_spec_exists=project_spec is not None,
        project_spec_type=type(project_spec).__name__ if project_spec else None,
        project_spec_name=project_spec.get("name") if isinstance(project_spec, dict) else None,
        project_spec_keys=list(project_spec.keys()) if isinstance(project_spec, dict) else None,
    )

    engineering_graph = create_engineering_subgraph()

    # Prepare initial state for the subgraph
    subgraph_input = {
        "messages": state.get("messages", []),
        "current_project": state.get("current_project"),
        "project_spec": state.get("project_spec"),
        "allocated_resources": state.get("allocated_resources", {}),
        "repo_info": state.get("repo_info"),
        "project_complexity": state.get("project_complexity"),
        "architect_complete": state.get("architect_complete", False),
        # Preparer state
        "selected_modules": state.get("selected_modules"),
        "deployment_hints": state.get("deployment_hints"),
        "custom_task_instructions": state.get("custom_task_instructions"),
        "repo_prepared": state.get("repo_prepared", False),
        "preparer_commit_sha": state.get("preparer_commit_sha"),
        # Engineering loop state
        "engineering_status": "working",
        "review_feedback": None,
        "iteration_count": 0,
        "test_results": None,
        "needs_human_approval": False,
        "human_approval_reason": None,
        "errors": state.get("errors", []),
    }

    # Run the subgraph
    result = await engineering_graph.ainvoke(subgraph_input)

    # Return the merged state
    return {
        "messages": result.get("messages", []),
        "repo_info": result.get("repo_info"),
        "project_complexity": result.get("project_complexity"),
        "architect_complete": result.get("architect_complete", False),
        # Preparer state
        "selected_modules": result.get("selected_modules"),
        "deployment_hints": result.get("deployment_hints"),
        "custom_task_instructions": result.get("custom_task_instructions"),
        "repo_prepared": result.get("repo_prepared", False),
        "preparer_commit_sha": result.get("preparer_commit_sha"),
        # Engineering loop state
        "engineering_status": result.get("engineering_status", "idle"),
        "review_feedback": result.get("review_feedback"),
        "engineering_iterations": result.get("iteration_count", 0),
        "test_results": result.get("test_results"),
        "needs_human_approval": result.get("needs_human_approval", False),
        "human_approval_reason": result.get("human_approval_reason"),
        "errors": result.get("errors", []),
        "current_agent": "engineering",
    }


def route_after_analyst(state: OrchestratorState) -> str | list[Send]:
    """Decide where to go after analyst.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If project was created -> dispatch Zavhoz + Engineering in parallel
    - Otherwise -> END (waiting for user input)
    """
    import structlog

    logger = structlog.get_logger()

    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools (e.g., create_project)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "analyst_tools"

    # If project was created, dispatch resources + engineering in parallel
    if state.get("current_project"):
        # Debug: log the state before dispatching
        project_spec = state.get("project_spec")
        logger.info(
            "route_after_analyst_dispatching",
            current_project=state.get("current_project"),
            project_spec_exists=project_spec is not None,
            project_spec_type=type(project_spec).__name__ if project_spec else None,
            project_spec_name=project_spec.get("name") if isinstance(project_spec, dict) else None,
        )
        # Explicitly pass project_spec to ensure it's available in subgraph
        # This works around potential state propagation issues with Send()
        engineering_state = {
            "project_spec": project_spec,
            "current_project": state.get("current_project"),
        }
        return [
            Send("zavhoz", {}),
            Send("engineering", engineering_state),
        ]

    # Otherwise END - LLM responded with a question, wait for user
    return END


def create_graph() -> StateGraph:
    """Create the orchestrator graph.

    Topology (Phase 3 & 4):
        START -> product_owner -> brainstorm -> [zavhoz || engineering]
                                              -> devops -> END
        START -> provisioner -> END (standalone)
    """
    graph = StateGraph(OrchestratorState)

    # Add nodes
    graph.add_node("product_owner", product_owner.run)
    graph.add_node("product_owner_tools", product_owner.execute_tools)
    # Brainstorm node removed
    graph.add_node("zavhoz", zavhoz.run)
    graph.add_node("zavhoz_tools", zavhoz.execute_tools)
    graph.add_node("engineering", run_engineering_subgraph)
    graph.add_node("devops", devops.run)
    graph.add_node("provisioner", provisioner.run)
    graph.add_node("analyst", analyst.run)
    graph.add_node("analyst_tools", analyst.execute_tools)

    # Start routing logic
    def route_start(state: OrchestratorState) -> str:
        """Route from start based on intent."""
        if state.get("server_to_provision"):
            return "provisioner"
        return "product_owner"

    graph.add_conditional_edges(
        START,
        route_start,
        {
            "product_owner": "product_owner",
            "provisioner": "provisioner",
        },
    )

    # After product owner: either execute tools, go to analyst, or end
    graph.add_conditional_edges(
        "product_owner",
        route_after_product_owner,
        {
            "product_owner_tools": "product_owner_tools",
            "analyst": "analyst",
            END: END,
        },
    )

    # After product owner tools: go to analyst, engineering (maintenance), or end
    graph.add_conditional_edges(
        "product_owner_tools",
        route_after_product_owner_tools,
        {
            "zavhoz": "zavhoz",
            "engineering": "engineering",
            "analyst": "analyst",
            END: END,
        },
    )

    # After analyst: either execute tools, dispatch parallel, or end
    graph.add_conditional_edges(
        "analyst",
        route_after_analyst,
    )

    # After analyst tools execution: back to analyst to process result
    graph.add_edge("analyst_tools", "analyst")

    # After zavhoz: either execute tools or end
    graph.add_conditional_edges(
        "zavhoz",
        route_after_zavhoz,
        {
            "zavhoz_tools": "zavhoz_tools",
            "devops": "devops",
            END: END,
        },
    )

    # After zavhoz tools execution: back to zavhoz to process result
    graph.add_edge("zavhoz_tools", "zavhoz")

    # After engineering subgraph: either devops or end (waiting)
    graph.add_conditional_edges(
        "engineering",
        route_after_engineering,
        {
            "devops": "devops",
            END: END,
        },
    )

    # After devops: END
    graph.add_edge("devops", END)

    # Provisioner: END (standalone operation)
    graph.add_edge("provisioner", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)
