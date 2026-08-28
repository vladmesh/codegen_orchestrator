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
    assert steps.index("Resolve the suite") < steps.index("Preflight ephemeral machines")


def test_the_machine_manifest_is_collected_even_when_creation_failed():
    """The created resource ids are the recovery input when a later step fails."""
    for name in ("Record machine manifest", "Upload the lifecycle manifest"):
        assert _steps()[name]["if"] == "always()", name


def test_the_matrix_fits_in_the_job_timeout():
    """Four full pipeline runs, each up to 45 minutes by the runner's own cap."""
    job = _workflow()["jobs"]["e2e"]

    assert job["timeout-minutes"] >= 180


def test_lifecycle_preflight_and_create_replace_the_static_host():
    workflow = WORKFLOW.read_text()
    steps = _steps()

    assert "secrets.PROD_HOST" not in workflow
    assert "secrets.ORCHESTRATOR_PUBLIC_IP" not in workflow
    assert "secrets.ORCHESTRATOR_HOSTNAME" not in workflow
    assert "stand-register" not in workflow
    assert "stand-self" not in workflow
    assert (
        "python3 -m scripts.stand_lifecycle preflight"
        in steps["Preflight ephemeral machines"]["run"]
    )
    assert "python3 -m scripts.stand_lifecycle create" in steps["Create ephemeral machines"]["run"]


def test_machine_ids_are_recorded_then_cleaned_for_every_terminal_outcome():
    steps = _steps()
    cleanup = _workflow()["jobs"]["cleanup"]

    assert steps["Record machine manifest"]["if"] == "always()"
    assert "always()" in cleanup["if"]
    assert "python3 -m scripts.stand_lifecycle cleanup" in cleanup["steps"][1]["run"]
    assert (
        "python3 -m scripts.stand_lifecycle sweep"
        in _workflow()["jobs"]["ttl-sweep"]["steps"][1]["run"]
    )


def test_later_steps_use_the_created_orchestrator_address():
    workflow = WORKFLOW.read_text()

    assert "steps.create.outputs.orchestrator_ip" in workflow
    assert "steps.create.outputs.target_ip" in workflow
