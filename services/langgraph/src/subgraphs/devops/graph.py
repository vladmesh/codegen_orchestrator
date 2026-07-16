"""DevOps subgraph builder.

Handles intelligent secret classification and deployment.
Returns missing user secrets to Product Owner if needed.

Topology:
    START -> env_analyzer -> secret_resolver -> readiness_check
                                                      |
                                      [if missing] -> END (return to PO)
                                      [if ready]  -> deployer
                                                        |
                                        [if errors] -> END
                                        [if ok]    -> smoke_tester -> END
"""

from typing import Any

from langgraph.graph import END, START, StateGraph
import structlog

from ...nodes.resource_allocator import resource_allocator_node
from .env_analyzer import env_analyzer_run
from .nodes import deployer_node, readiness_check_node, secret_resolver_node
from .secret_resolver import SecretResolutionError, TypedSecretResolutionError
from .smoke import smoke_tester_node
from .state import DevOpsState

logger = structlog.get_logger()


def route_after_env_analyzer(state: DevOpsState) -> str:
    """Route after env analysis."""
    if state.get("errors"):
        return END
    return "secret_resolver"


def route_after_secret_resolver(state: DevOpsState) -> str:
    """Route after secret resolution."""
    if state.get("errors"):
        return END
    return "readiness_check"


async def resolve_secrets(state: DevOpsState) -> dict:
    """Convert resolver validation errors into the deploy result error path."""
    try:
        return await secret_resolver_node.run(state)
    except TypedSecretResolutionError as error:
        logger.error("typed_secret_resolution_failed", outcome=error.outcome)
        return {"errors": [str(error)], "resolution_outcome": error.outcome}
    except SecretResolutionError as error:
        logger.error("secret_resolution_failed", error_type=type(error).__name__)
        return {"errors": [str(error)]}


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


def route_after_deployer(state: DevOpsState) -> str:
    """Route after deployer.

    - If deployed_url is set and no errors -> smoke_tester
    - Otherwise (deploy failed) -> END
    """
    if state.get("deployed_url") and not state.get("errors"):
        return "smoke_tester"
    return END


def create_devops_subgraph() -> Any:
    """Create the DevOps subgraph.

    Topology:
        START -> env_analyzer -> secret_resolver -> readiness_check
                                                          |
                                          [if missing] -> END
                                          [if ready]  -> deployer
                                                            |
                                            [if errors] -> END
                                            [if ok]    -> smoke_tester -> END
    """
    graph = StateGraph(DevOpsState)

    # Add nodes
    graph.add_node("resource_allocator", resource_allocator_node.run)
    graph.add_node("env_analyzer", env_analyzer_run)
    graph.add_node("secret_resolver", resolve_secrets)
    graph.add_node("readiness_check", readiness_check_node.run)
    graph.add_node("deployer", deployer_node.run)
    graph.add_node("smoke_tester", smoke_tester_node.run)

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
            END: END,
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

    graph.add_conditional_edges(
        "deployer",
        route_after_deployer,
        {
            "smoke_tester": "smoke_tester",
            END: END,
        },
    )

    graph.add_edge("smoke_tester", END)

    return graph.compile()
