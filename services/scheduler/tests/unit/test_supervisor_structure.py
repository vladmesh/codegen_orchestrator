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
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def test_deploy_supervisor_entrypoint_is_selection_and_aggregation_only():
    source = DEPLOY_SUPERVISOR.read_text()
    function = _async_function(source, "supervise_deploying_stories")
    signature = source.splitlines()[function.lineno - 1]

    assert "noqa" not in signature
    calls = {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_supervise_deploying_story" in calls
    assert not {
        "_handle_deploy_success_story",
        "_handle_deploy_code_fix",
        "_handle_deploy_retry",
        "_route_refused_deploy",
        "_handle_deploy_waiting_user_secret",
        "_handle_deploy_give_up",
    } & calls


def test_deploy_outcome_router_covers_the_contract():
    deploy = importlib.import_module("src.tasks.supervisor.deploy")

    assert deploy._ROUTED_DEPLOY_OUTCOMES == frozenset(DeployOutcome)
