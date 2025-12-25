"""LangGraph graph definition."""

from typing import Annotated

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Send
from typing_extensions import TypedDict

from .nodes import brainstorm, devops, product_owner, provisioner, zavhoz
from .subgraphs.engineering import create_engineering_subgraph


class OrchestratorState(TypedDict):
    """Global state for the orchestrator."""

    # Messages (conversation history)
    messages: Annotated[list, add_messages]

    # Current project
    current_project: str | None
    project_spec: dict | None
    project_intent: dict | None
    po_intent: str | None

    # Resources (handle -> resource_id mapping)
    allocated_resources: dict

    # Repository info (after architect creates it)
    repo_info: dict | None
    project_complexity: str | None
    architect_complete: bool

    # Engineering subgraph tracking (Phase 3)
    engineering_status: str  # "idle" | "working" | "done" | "blocked"
    review_feedback: str | None
    engineering_iterations: int
    test_results: dict | None

    # Human-in-the-loop (Phase 4)
    needs_human_approval: bool
    human_approval_reason: str | None

    # Provisioning
    server_to_provision: str | None  # Server handle to provision
    is_incident_recovery: bool  # If True, redeploy services after
    provisioning_result: dict | None  # Result from provisioner

    # Status
    current_agent: str
    errors: list[str]

    # Results
    deployed_url: str | None


def route_after_brainstorm(state: OrchestratorState) -> str | list[Send]:
    """Decide where to go after brainstorm.

    Routing logic:
    - If LLM made tool calls -> execute them
    - If project was created -> dispatch Zavhoz + Engineering in parallel
    - Otherwise -> END (waiting for user input)
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools (e.g., create_project)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "brainstorm_tools"

    # If project was created, dispatch resources + engineering in parallel
    if state.get("current_project"):
        return [
            Send("zavhoz", {}),
            Send("engineering", {}),
        ]

    # Otherwise END - LLM responded with a question, wait for user
    return END


def route_after_zavhoz(state: OrchestratorState) -> str:
    """Decide where to go after zavhoz.

    Routing logic:
    - If LLM made tool calls -> execute them
    - Otherwise -> END (Engineering runs in parallel)
    """
    messages = state.get("messages", [])
    if not messages:
        return END

    last_message = messages[-1]

    # If LLM wants to call tools (e.g., find_suitable_server, allocate_port)
    if hasattr(last_message, "tool_calls") and last_message.tool_calls:
        return "zavhoz_tools"

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
        return "brainstorm"

    return END


def route_after_product_owner_tools(state: OrchestratorState) -> str:
    """Decide where to go after product owner tools execution."""
    po_intent = state.get("po_intent")
    
    if po_intent == "new_project":
        return "brainstorm"
    
    if po_intent == "maintenance":
        # Project update → Engineering directly (skip brainstorm)
        return "engineering"
    
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
        "engineering_status": result.get("engineering_status", "idle"),
        "review_feedback": result.get("review_feedback"),
        "engineering_iterations": result.get("iteration_count", 0),
        "test_results": result.get("test_results"),
        "needs_human_approval": result.get("needs_human_approval", False),
        "human_approval_reason": result.get("human_approval_reason"),
        "errors": result.get("errors", []),
        "current_agent": "engineering",
    }


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
    graph.add_node("brainstorm", brainstorm.run)
    graph.add_node("brainstorm_tools", brainstorm.execute_tools)
    graph.add_node("zavhoz", zavhoz.run)
    graph.add_node("zavhoz_tools", zavhoz.execute_tools)
    graph.add_node("engineering", run_engineering_subgraph)
    graph.add_node("devops", devops.run)
    graph.add_node("provisioner", provisioner.run)

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

    # After product owner: either execute tools, go to brainstorm, or end
    graph.add_conditional_edges(
        "product_owner",
        route_after_product_owner,
        {
            "product_owner_tools": "product_owner_tools",
            "brainstorm": "brainstorm",
            END: END,
        },
    )

    # After product owner tools: go to brainstorm, engineering (maintenance), or end
    graph.add_conditional_edges(
        "product_owner_tools",
        route_after_product_owner_tools,
        {
            "brainstorm": "brainstorm",
            "engineering": "engineering",
            END: END,
        },
    )

    # After brainstorm: either execute tools, dispatch parallel, or end
    graph.add_conditional_edges(
        "brainstorm",
        route_after_brainstorm,
    )

    # After brainstorm tools execution: back to brainstorm to process result
    graph.add_edge("brainstorm_tools", "brainstorm")

    # After zavhoz: either execute tools or end
    graph.add_conditional_edges(
        "zavhoz",
        route_after_zavhoz,
        {
            "zavhoz_tools": "zavhoz_tools",
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
