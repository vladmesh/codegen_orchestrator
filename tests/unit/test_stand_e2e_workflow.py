"""The e2e button: what it must not get wrong.

It exists so a suite can be started without an SSH key at hand. The properties
below are the ones whose absence is discovered at the worst moment — a second
run trampling the first, or a failed run whose logs were never collected.
"""

from pathlib import Path

import yaml

from scripts.stand_acceptance import PROTECTED_STAND_SECRET_NAMES

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


def test_the_machine_manifest_is_collected_and_scanned_before_handoff_upload():
    """The created resource ids are the recovery input when a later step fails."""
    assert _steps()["Record machine manifest"]["if"] == "always()"
    assert _steps()["Admit cleanup handoff"]["if"] == "always()"
    assert _steps()["Upload cleanup handoff"]["if"] == (
        "${{ always() && steps.handoff-admission.outcome == 'success' }}"
    )


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


def test_credential_preflight_refuses_before_the_provider_preflight_or_create():
    steps = list(_steps())
    credentials = _steps()["Validate pre-create credentials"]
    lifecycle = _steps()["Preflight ephemeral machines"]

    assert "python3 -m scripts.stand_credentials" in credentials["run"]
    assert steps.index("Validate pre-create credentials") < steps.index(
        "Preflight ephemeral machines"
    )
    assert "python3 -m scripts.stand_lifecycle preflight" in lifecycle["run"]
    for secret in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        "CODEX_ACCESS_TOKEN",
    ):
        assert secret in credentials["env"]
        assert secret not in credentials["run"]


def test_machine_ids_are_recorded_then_cleaned_for_every_terminal_outcome():
    steps = _steps()
    cleanup = _workflow()["jobs"]["cleanup"]

    assert steps["Record machine manifest"]["if"] == "always()"
    assert "always()" in cleanup["if"]
    cleanup_steps = {step["name"]: step for step in cleanup["steps"]}
    assert (
        "python3 -m scripts.stand_lifecycle cleanup-report"
        in cleanup_steps["Cleanup and observe run-tagged machines"]["run"]
    )
    assert (
        "python3 -m scripts.stand_lifecycle sweep"
        in _workflow()["jobs"]["ttl-sweep"]["steps"][1]["run"]
    )


def test_worker_image_fallback_is_limited_to_confirmed_missing_releases():
    job = _workflow()["jobs"]["e2e"]
    step = _steps()["Provide worker base images on the stand"]

    assert job["permissions"]["packages"] == "read"
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert "${GITHUB_SHA}" in step["run"]
    assert "git merge-base HEAD origin/main" not in step["run"]
    assert "GHCR_TOKEN='${GHCR_TOKEN}'" not in step["run"]
    assert "read -r GHCR_TOKEN" in step["run"]
    assert 'case "${pulled}"' in step["run"]
    assert "9)" in step["run"]
    assert "5|9)" not in step["run"]
    assert "FATAL: pulling the worker image release failed" in step["run"]


def test_machine_cleanup_has_no_retention_escape_hatch():
    workflow = WORKFLOW.read_text()

    assert "keep_machines" not in workflow
    assert "Cleanup and observe run-tagged machines" in workflow


def test_selected_suite_runs_through_the_supported_remote_runner_and_preserves_failure():
    steps = _steps()
    run = steps["Run selected stand suite"]

    assert "scripts/stand_run.py" in run["run"]
    assert "--suite %q" in run["run"]
    assert '"${SUITE}"' in run["run"]
    assert '"${WORKER}"' in run["run"]
    assert '"${QA}"' in run["run"]
    assert run["continue-on-error"] is True
    assert _steps()["Preserve suite result"]["if"] == "always()"


def test_remote_runner_is_provisioned_and_only_runs_after_target_provisioning():
    steps = _steps()
    run = steps["Run selected stand suite"]
    provision = (
        WORKFLOW.parents[2] / "services/infra-service/ansible/playbooks/provision_software.yml"
    ).read_text()
    provision_vars = (
        WORKFLOW.parents[2] / "services/infra-service/ansible/group_vars/provision_vars.yml"
    ).read_text()

    assert "Install pinned uv for stand runner" in provision
    assert "uv_version" in provision_vars
    assert "uv --version" in steps["Bootstrap dynamic orchestrator"]["run"]
    assert run["if"] == "success()"
    assert "remote-invocation.log" in run["run"]
    assert ">/dev/null 2>&1" not in run["run"]


def test_final_evidence_is_built_after_always_cleanup_for_success_failure_and_cancellation():
    workflow = _workflow()
    cleanup = workflow["jobs"]["cleanup"]
    steps = {step["name"]: step for step in cleanup["steps"]}

    assert "always()" in cleanup["if"]
    assert cleanup["needs"] == "e2e"
    assert "cleanup-report" in steps["Cleanup and observe run-tagged machines"]["run"]
    assert steps["Build final acceptance evidence"]["continue-on-error"] is True
    assert "continue-on-error" not in steps["Admit final artifact"]
    uploads = [
        step for step in cleanup["steps"] if "uses" in step and "upload-artifact" in step["uses"]
    ]
    assert len(uploads) == 1
    assert uploads[0]["if"] == "${{ always() && steps.final-admission.outcome == 'success' }}"


def test_artifact_uploads_have_an_attempt_name_and_an_explicit_always_admission_boundary():
    workflow = _workflow()
    e2e_steps = _steps()
    cleanup_steps = {step["name"]: step for step in workflow["jobs"]["cleanup"]["steps"]}

    for step in (e2e_steps["Upload cleanup handoff"], cleanup_steps["Upload acceptance artifact"]):
        assert "github.run_attempt" in step["with"]["name"]
        assert step["if"].startswith("${{ always() &&")
        assert "success()" not in step["if"]
    assert "--protected-env" in e2e_steps["Admit cleanup handoff"]["run"]
    assert "--protected-env" in cleanup_steps["Admit final artifact"]["run"]
    assert "never-upload" in e2e_steps["Admit cleanup handoff"]["run"]
    assert "never-upload" in cleanup_steps["Admit final artifact"]["run"]


def test_admission_prevents_each_upload_only_when_it_fails_for_any_e2e_outcome():
    e2e_steps = _steps()
    cleanup_steps = {step["name"]: step for step in _workflow()["jobs"]["cleanup"]["steps"]}

    for step, admission in (
        (e2e_steps["Upload cleanup handoff"], "handoff-admission"),
        (cleanup_steps["Upload acceptance artifact"], "final-admission"),
    ):
        condition = step["if"]
        assert condition == f"${{{{ always() && steps.{admission}.outcome == 'success' }}}}"
        assert "build-evidence" not in condition
        assert "handoff.outcome" not in condition


def test_each_admission_receives_the_complete_protected_value_environment_and_reports_rejections():
    workflow = _workflow()
    e2e_admission = _steps()["Admit cleanup handoff"]
    final_admission = {step["name"]: step for step in workflow["jobs"]["cleanup"]["steps"]}[
        "Admit final artifact"
    ]

    for step in (e2e_admission, final_admission):
        assert set(step["env"]) == PROTECTED_STAND_SECRET_NAMES
        assert '--summary "${GITHUB_STEP_SUMMARY}"' in step["run"]
    assert "--secrets-stdin" not in WORKFLOW.read_text()


def test_later_steps_use_the_created_orchestrator_address():
    workflow = WORKFLOW.read_text()

    assert "steps.create.outputs.orchestrator_ip" in workflow
    assert "steps.create.outputs.target_ip" in workflow


def test_created_pair_is_bootstrapped_registered_and_provisioned_without_secret_outputs():
    """The workflow must turn the dynamic pair into the contour it later tests."""
    workflow = WORKFLOW.read_text()
    steps = _steps()

    for output in (
        "orchestrator_ip",
        "target_ip",
        "target_id",
    ):
        assert f"steps.create.outputs.{output}" in workflow

    assert "Bootstrap dynamic orchestrator" in steps
    assert "Register and provision dynamic target" in steps
    assert steps["Bootstrap dynamic orchestrator"]["env"]["PROD_HOST"] == (
        "${{ steps.create.outputs.orchestrator_ip }}"
    )
    assert (
        "python3 -m scripts.register_bitlaunch_target"
        in steps["Register and provision dynamic target"]["run"]
    )
    assert (
        "python3 -m scripts.request_stand_provisioning"
        in steps["Register and provision dynamic target"]["run"]
    )
    assert "machines.json" not in steps["Bootstrap dynamic orchestrator"]["run"]
    assert "machines.json" not in steps["Register and provision dynamic target"]["run"]
    assert "GITHUB_OUTPUT" not in steps["Bootstrap dynamic orchestrator"]["run"]
    assert "GITHUB_OUTPUT" not in steps["Register and provision dynamic target"]["run"]


def test_bootstrap_installs_the_pinned_uv_toolchain_before_any_uvx_invocation():
    steps = list(_steps())
    bootstrap = _steps()["Bootstrap dynamic orchestrator"]

    assert "Install uv" in steps
    assert steps.index("Install uv") < steps.index("Bootstrap dynamic orchestrator")
    assert _steps()["Install uv"]["uses"] == "astral-sh/setup-uv@v7"
    assert "uvx --from ansible-core" in bootstrap["run"]


def test_dynamic_stand_environment_mounts_the_github_app_key_and_names_the_contour():
    step = _steps()["Render protected dynamic configuration"]
    render = step["run"]

    assert '"GITHUB_APP_PEM_PATH"' in render
    assert '"GITHUB_APP_PRIVATE_KEY_PATH"' in render
    assert '"LIVE_CONTOUR"' in render
    assert step["env"]["GITHUB_APP_PEM_PATH"] == "/opt/secrets/github_app.pem"
    assert step["env"]["GITHUB_APP_PRIVATE_KEY_PATH"] == "/app/keys/github_app.pem"
    assert step["env"]["LIVE_CONTOUR"] == "stand"


def test_dynamic_stand_configuration_receives_tokens_only_as_protected_manager_settings():
    step = _steps()["Render protected dynamic configuration"]
    render = step["run"]

    for name in (
        "STAND_CLAUDE_CODE_OAUTH_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
        "STAND_CODEX_ACCESS_TOKEN",
    ):
        assert name in step["env"]
        assert f'"{name}"' in render
    assert "HOST_CLAUDE_DIR" not in step["env"]
    assert "HOST_CODEX_HOME" not in step["env"]


def test_stand_overlay_removes_worker_manager_host_session_mounts():
    overlay = (WORKFLOW.parents[2] / "docker-compose.stand.yml").read_text()

    assert 'HOST_CLAUDE_DIR: ""' in overlay
    assert 'HOST_CODEX_HOME: ""' in overlay
    assert "/host-claude" not in overlay
    assert "/host-codex" not in overlay


def test_target_key_transport_uses_protected_files_not_a_sourced_secret_environment():
    register = _steps()["Register and provision dynamic target"]["run"]

    assert "/run/stand-target.key" in register
    assert "--ssh-private-key-file /run/stand-target.key" in register
    assert "set -a; . /run/stand-target.env; set +a" not in register
    assert "trap cleanup EXIT INT TERM" in register
    assert "shred -u /run/stand-target.key /run/stand-target.json" in register
    assert "SSH_PRIVATE_KEY" not in register.split("ssh -i", maxsplit=1)[1]


def test_obsolete_self_target_registration_route_is_deleted():
    assert not (WORKFLOW.parents[2] / "scripts" / "register_stand_target.py").exists()
