"""Regression coverage for the scheduler supervisor module boundaries."""

import importlib
from pathlib import Path

SUPERVISOR_ROOT = Path(__file__).parents[2] / "src/tasks"


def test_supervisor_is_a_package_with_a_small_runtime_facade():
    assert not (SUPERVISOR_ROOT / "supervisor.py").exists()

    supervisor = importlib.import_module("src.tasks.supervisor")
    assert {name for name in supervisor.__all__ if name.startswith("supervise_")} == {
        "supervise_deploying_stories",
        "supervise_failed_tasks",
        "supervise_stuck_stories",
        "supervise_stuck_tasks",
        "supervise_testing_stories",
        "supervise_waiting_resource_tasks",
        "supervise_waiting_user_secret_stories",
    }
    assert "STORY_RETRY_KEY_PREFIX" in supervisor.__all__

    for module in ("common", "handoff", "liveness", "deploy", "qa"):
        assert (SUPERVISOR_ROOT / "supervisor" / f"{module}.py").exists()
