"""The e2e button: what it must not get wrong.

It exists so a suite can be started without an SSH key at hand. The properties
below are the ones whose absence is discovered at the worst moment — a second
run trampling the first, or a failed run whose logs were never collected.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "stand-e2e.yml"


def _workflow() -> dict:
    loaded = yaml.safe_load(WORKFLOW.read_text())
    # YAML reads a bare `on:` key as the boolean True.
    loaded["on"] = loaded.get("on", loaded.get(True))
    return loaded


def _steps() -> dict[str, dict]:
    return {step["name"]: step for step in _workflow()["jobs"]["e2e"]["steps"]}


def test_it_runs_on_the_stand_and_nowhere_else():
    assert _workflow()["jobs"]["e2e"]["environment"] == "stand"


def test_only_one_e2e_at_a_time():
    """They share a stand, a target server and one subscription per agent."""
    concurrency = _workflow()["concurrency"]

    assert concurrency["group"] == "stand-e2e"
    assert concurrency["cancel-in-progress"] is False


def test_every_named_suite_is_offered_plus_an_arbitrary_target():
    options = _workflow()["on"]["workflow_dispatch"]["inputs"]["suite"]["options"]

    assert {"mega", "llm", "matrix"} <= set(options)
    assert "custom" in options, "an e2e invented later must be startable without a code change"


def test_a_custom_suite_without_a_target_is_refused_before_anything_runs():
    steps = list(_steps())
    resolve = _steps()["Resolve the suite"]

    assert "exit 1" in resolve["run"]
    assert steps.index("Resolve the suite") < steps.index("Run it on the stand")


def test_the_run_directory_is_collected_even_when_the_suite_failed():
    """A failed run is precisely the one whose logs are worth keeping."""
    for name in ("Collect the run directory", "Upload the run directory"):
        assert _steps()[name]["if"] == "always()", name


def test_the_matrix_fits_in_the_job_timeout():
    """Four full pipeline runs, each up to 45 minutes by the runner's own cap."""
    job = _workflow()["jobs"]["e2e"]

    assert job["timeout-minutes"] >= 180
