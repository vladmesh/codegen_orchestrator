"""DevOps subgraph builder.

Handles intelligent secret classification and deployment.
Returns missing user secrets to Product Owner if needed.

Topology:
    START -> env_analyzer -> secret_resolver -> readiness_check
                                                      |
                                      [if missing] -> END (return to PO)
                                      [if ready]  -> deployer -> END
"""

from typing import Any

from langgraph.graph import END, START, StateGraph
import structlog

from ...nodes.resource_allocator import resource_allocator_node
from .env_analyzer import env_analyzer_run
from .nodes import deployer_node, readiness_check_node, secret_resolver_node
from .state import DevOpsState

logger = structlog.get_logger()


def route_after_env_analyzer(state: DevOpsState) -> str:
    """Route after env analysis."""
    if state.get("errors"):
        return END
    return "secret_resolver"


def route_after_secret_resolver(state: DevOpsState) -> str:
    """Route after secret resolution."""
    return "readiness_check"


def route_after_readiness_check(state: DevOpsState) -> str:
    """Route after readiness check.

    - If missing secrets -> END (return to PO)
    - If ready -> deployer
    """
    missing = state.get("missing_user_secrets", [])

    if missing:
        logger.info(
            "route_to_end_missing_secrets",
            missing_count=len(missing),
        )
        return END

    return "deployer"


def create_devops_subgraph() -> Any:
    """Create the DevOps subgraph.

    Topology:
        START -> env_analyzer -> secret_resolver -> readiness_check
                                                          |
                                          [if missing] -> END
                                          [if ready]  -> deployer -> END
    """
    graph = StateGraph(DevOpsState)

    # Add nodes
    # Add nodes
    graph.add_node("resource_allocator", resource_allocator_node.run)
    graph.add_node("env_analyzer", env_analyzer_run)
    graph.add_node("secret_resolver", secret_resolver_node.run)
    graph.add_node("readiness_check", readiness_check_node.run)
    graph.add_node("deployer", deployer_node.run)

    # Edges
    graph.add_edge(START, "resource_allocator")
    graph.add_edge("resource_allocator", "env_analyzer")

    graph.add_conditional_edges(
        "env_analyzer",
        route_after_env_analyzer,
        {
            "secret_resolver": "secret_resolver",
            END: END,
        },
    )

    graph.add_conditional_edges(
        "secret_resolver",
        route_after_secret_resolver,
        {
            "readiness_check": "readiness_check",
        },
    )

    graph.add_conditional_edges(
        "readiness_check",
        route_after_readiness_check,
        {
            "deployer": "deployer",
            END: END,
        },
    )

    graph.add_edge("deployer", END)

    return graph.compile()
