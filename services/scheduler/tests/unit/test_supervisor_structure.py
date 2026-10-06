"""Regression coverage for the scheduler supervisor module boundaries."""

import ast
import importlib
from pathlib import Path

from shared.contracts.queues.deploy import DeployOutcome

SUPERVISOR_ROOT = Path(__file__).parents[2] / "src/tasks"
DEPLOY_SUPERVISOR = SUPERVISOR_ROOT / "supervisor/deploy.py"


def test_supervisor_is_a_package_with_a_small_runtime_facade():
    assert not (SUPERVISOR_ROOT / "supervisor.py").exists()

    supervisor = importlib.import_module("src.tasks.supervisor")
    assert set(supervisor.__all__) == {
        "supervise_application_deploy_handoffs",
        "supervise_deploying_stories",
        "supervise_failed_tasks",
        "supervise_stage_notices",
        "supervise_state_age_bounds",
        "supervise_stuck_stories",
        "supervise_stuck_tasks",
        "supervise_testing_stories",
        "supervise_waiting_resource_tasks",
        "supervise_waiting_user_secret_stories",
    }
    for module in ("common", "handoff", "liveness", "deploy", "qa", "state_age", "stage_notices"):
        assert (SUPERVISOR_ROOT / "supervisor" / f"{module}.py").exists()


def _async_function(source: str, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(source)
    return next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def test_deploy_outcome_router_covers_the_contract():
    deploy = importlib.import_module("src.tasks.supervisor.deploy")

    assert deploy._ROUTED_DEPLOY_OUTCOMES == frozenset(DeployOutcome)
