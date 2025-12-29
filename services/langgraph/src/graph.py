"""LangGraph graph definition."""

from typing import Annotated

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from .nodes import analyst, devops, intent_parser, product_owner, provisioner, zavhoz
from .subgraphs.engineering import create_engineering_subgraph

# Maximum iterations for PO agentic loop before forcing END
MAX_PO_ITERATIONS = 20


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


def _merge_capabilities(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Reducer that merges capability lists."""
    left = left or []
    right = right or []
    return list(set(left) | set(right))


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
    # DYNAMIC PO (Phase 2 + 3)
    # ============================================================
    # Thread ID for checkpointing and session tracking
    thread_id: str | None
    # Active capabilities loaded by intent parser
    active_capabilities: Annotated[list[str], _merge_capabilities]
    # Brief task summary from intent parser
    task_summary: str | None
    # Flag: skip intent parser (set when continuing session)
    skip_intent_parser: bool
    # Telegram chat ID (needed by respond_to_user tool)
    chat_id: int | None
    # Correlation ID for distributed tracing
    correlation_id: str | None
    # Phase 3: Agentic loop control
    awaiting_user_response: bool  # Waiting for user input?
    user_confirmed_complete: bool  # User said done (finish_task called)?
    po_iterations: int  # Loop counter (max 20)

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

    # If LLM wants to call tools (e.g., find_suitable_server, allocate_port)
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

    # If resources allocated, proceed based on intent
    if allocated:
        if po_intent == "deploy":
            # Direct deploy request -> DevOps
            return "devops"
        # New project or other -> Engineering first, then DevOps
        return "engineering"

    # Resources not allocated - Zavhoz didn't complete allocation
    # Log warning and end (user will need to retry or check logs)
    logger.warning(
        "zavhoz_no_resources_allocated",
        po_intent=po_intent,
        hint="Zavhoz did not allocate resources. Check Zavhoz prompt/tools.",
    )
    return END


def route_after_intent_parser(state: OrchestratorState) -> str:
    """After intent parser, always go to product owner."""
    return "product_owner"


def route_after_product_owner(state: OrchestratorState) -> str:
    """Decide where to go after product owner.

    Phase 3 agentic loop routing:
    - If task complete -> END
    - If awaiting user response -> END (checkpoint saves state)
    - If max iterations -> END
    - If has tool calls -> execute them
    - Otherwise -> END
    """
    import structlog

    logger = structlog.get_logger()

    # Task complete?
    if state.get("user_confirmed_complete"):
        logger.info("po_routing_task_complete")
        return END

    # Waiting for user?
    if state.get("awaiting_user_response"):
        logger.info("po_routing_awaiting_user")
        return END

    # Max iterations?
    iterations = state.get("po_iterations", 0)
    if iterations >= MAX_PO_ITERATIONS:
        logger.warning("po_routing_max_iterations", iterations=iterations)
        return END

    # Has tool calls?
    messages = state.get("messages", [])
    if messages:
        last_message = messages[-1]
        if hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "product_owner_tools"

    return END


def route_after_product_owner_tools(state: OrchestratorState) -> str:
    """Decide where to go after product owner tools execution.

    Phase 3 agentic loop:
    - If task complete -> END
    - If awaiting user -> END
    - Otherwise -> back to PO for next iteration
    """
    # Task complete?
    if state.get("user_confirmed_complete"):
        return END

    # Waiting for user?
    if state.get("awaiting_user_response"):
        return END

    # Continue agentic loop - back to PO
    return "product_owner"


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


def route_after_analyst(state: OrchestratorState) -> str:
    """Decide where to go after analyst.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If project was created -> go to Zavhoz for resource allocation
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

    # If project was created, go to Zavhoz first (sequential flow)
    # Zavhoz allocates resources, then Engineering builds, then DevOps deploys
    if state.get("current_project"):
        project_spec = state.get("project_spec")
        logger.info(
            "route_after_analyst_to_zavhoz",
            current_project=state.get("current_project"),
            project_spec_exists=project_spec is not None,
            project_spec_name=project_spec.get("name") if isinstance(project_spec, dict) else None,
        )
        return "zavhoz"

    # Otherwise END - LLM responded with a question, wait for user
    return END


def route_after_devops(state: OrchestratorState) -> str:
    """Decide where to go after devops.

    Routing logic:
    - If LLM made tool calls -> execute them
    - Otherwise -> END (deployment finished or question asking)
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools (e.g., analyze_env_requirements, run_ansible_deploy)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "devops_tools"

    return END


def create_graph() -> StateGraph:
    """Create the orchestrator graph.

    Topology (Dynamic PO):
        START -> intent_parser -> product_owner -> ... -> END
        START -> product_owner (if skip_intent_parser) -> ... -> END
        START -> provisioner -> END (standalone)
    """
    graph = StateGraph(OrchestratorState)

    # Add nodes
    graph.add_node("intent_parser", intent_parser.run)
    graph.add_node("product_owner", product_owner.run)
    graph.add_node("product_owner_tools", product_owner.execute_tools)
    graph.add_node("zavhoz", zavhoz.run)
    graph.add_node("zavhoz_tools", zavhoz.execute_tools)
    graph.add_node("engineering", run_engineering_subgraph)
    graph.add_node("devops", devops.run)
    graph.add_node("devops_tools", devops.execute_tools)
    graph.add_node("provisioner", provisioner.run)
    graph.add_node("analyst", analyst.run)
    graph.add_node("analyst_tools", analyst.execute_tools)

    # Start routing logic
    def route_start(state: OrchestratorState) -> str:
        """Route from start based on intent."""
        if state.get("server_to_provision"):
            return "provisioner"
        # Skip intent parser if continuing existing session
        if state.get("skip_intent_parser"):
            return "product_owner"
        return "intent_parser"

    graph.add_conditional_edges(
        START,
        route_start,
        {
            "intent_parser": "intent_parser",
            "product_owner": "product_owner",
            "provisioner": "provisioner",
        },
    )

    # After intent parser: always go to product owner
    graph.add_edge("intent_parser", "product_owner")

    # After product owner: execute tools or end (Phase 3 agentic loop)
    graph.add_conditional_edges(
        "product_owner",
        route_after_product_owner,
        {
            "product_owner_tools": "product_owner_tools",
            END: END,
        },
    )

    # After product owner tools: loop back to PO or end (Phase 3 agentic loop)
    graph.add_conditional_edges(
        "product_owner_tools",
        route_after_product_owner_tools,
        {
            "product_owner": "product_owner",
            END: END,
        },
    )

    # After analyst: either execute tools, go to zavhoz, or end
    graph.add_conditional_edges(
        "analyst",
        route_after_analyst,
        {
            "analyst_tools": "analyst_tools",
            "zavhoz": "zavhoz",
            END: END,
        },
    )

    # After analyst tools execution: back to analyst to process result
    graph.add_edge("analyst_tools", "analyst")

    # After zavhoz: execute tools, go to engineering (new project), or devops (deploy)
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

    # After devops: execute tools or end
    graph.add_conditional_edges(
        "devops",
        route_after_devops,
        {
            "devops_tools": "devops_tools",
            END: END,
        },
    )

    # After devops tools: back to devops
    graph.add_edge("devops_tools", "devops")

    # Provisioner: END (standalone operation)
    graph.add_edge("provisioner", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)
