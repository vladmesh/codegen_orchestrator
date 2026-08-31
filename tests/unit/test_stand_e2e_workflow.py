"""The e2e button: what it must not get wrong.

It exists so a suite can be started without an SSH key at hand. The properties
below are the ones whose absence is discovered at the worst moment — a second
run trampling the first, or a failed run whose logs were never collected.
"""

from pathlib import Path

import yaml

from scripts.stand_acceptance import PROTECTED_STAND_SECRET_NAMES
from scripts.stand_run import (
    MATRIX_RUNNER_TIMEOUT_SECONDS,
    STAND_CLEANUP_JOB_TIMEOUT_MINUTES,
    STAND_JOB_TIMEOUT_MINUTES,
    STAND_PROVISIONING_TIMEOUT_SECONDS,
    SUITES,
)

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "stand-e2e.yml"
MAKEFILE = Path(__file__).parents[2] / "Makefile"
CONTROL_PLANE_PLAYBOOK = (
    WORKFLOW.parents[2]
    / "services/infra-service/ansible/playbooks/provision_stand_control_plane.yml"
)


def _workflow() -> dict:
    loaded = yaml.safe_load(WORKFLOW.read_text())
    # YAML reads a bare `on:` key as the boolean True.
    loaded["on"] = loaded.get("on", loaded.get(True))
    return loaded


def _steps() -> dict[str, dict]:
    return {step["name"]: step for step in _workflow()["jobs"]["e2e"]["steps"]}


def _sweep_steps() -> dict:
    return {
        step["name"]: step for step in _workflow()["jobs"]["ttl-sweep"]["steps"] if "name" in step
    }


def _cleanup_steps() -> dict:
    return {
        step["name"]: step for step in _workflow()["jobs"]["cleanup"]["steps"] if "name" in step
    }


def test_it_runs_on_the_stand_and_nowhere_else():
    assert _workflow()["jobs"]["e2e"]["environment"] == "stand"


def test_only_one_e2e_at_a_time():
    """They share a stand, a target server and one subscription per agent."""
    concurrency = _workflow()["concurrency"]

    assert concurrency["group"] == "stand-e2e"
    assert concurrency["cancel-in-progress"] is False


def test_every_named_suite_is_offered_plus_an_arbitrary_target():
    options = _workflow()["on"]["workflow_dispatch"]["inputs"]["suite"]["options"]

    assert set(options) == {"mega-noop", "mega-llm", "matrix", "custom"}
    assert "custom" in options, "an e2e invented later must be startable without a code change"


def test_workflow_suite_names_match_the_runner_canonical_suite_table():
    options = _workflow()["on"]["workflow_dispatch"]["inputs"]["suite"]["options"]

    assert set(options) - {"custom"} == set(SUITES)
    assert _workflow()["on"]["workflow_dispatch"]["inputs"]["suite"]["default"] == "mega-noop"
    assert "inputs.suite" in _workflow()["run-name"]


def test_worker_and_qa_inputs_describe_when_the_runner_uses_them():
    inputs = _workflow()["on"]["workflow_dispatch"]["inputs"]

    for name in ("worker", "qa"):
        description = inputs[name]["description"]
        assert "mega-llm" in description
        assert "matrix" in description
        assert "mega-noop" in description


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


def test_handoff_collects_only_logs_the_selected_suite_can_produce():
    """A one-cell suite must not spend SSH retries probing the other matrix cells."""
    collect = _steps()["Record machine manifest"]
    script = collect["run"]

    assert collect["env"]["SUITE"] == "${{ steps.suite.outputs.value }}"
    assert collect["env"]["WORKER"] == "${{ inputs.worker }}"
    assert collect["env"]["QA"] == "${{ inputs.qa }}"
    assert 'if [ "${SUITE}" = "matrix" ]' in script
    assert 'reports+=("${QA}-${WORKER}.log")' in script
    assert 'for name in "${reports[@]}"' in script


def test_worker_failure_evidence_is_copied_before_the_ephemeral_host_is_deleted():
    collect = _steps()["Record machine manifest"]["run"]

    assert "run-evidence-*.json" in collect
    assert 'tar -C "${run_dir}" -xf -' in collect


def test_the_matrix_fits_in_the_job_timeout():
    """The provisioned stand, four cells, and their cleanup reserve fit strictly."""
    job = _workflow()["jobs"]["e2e"]

    assert job["timeout-minutes"] == STAND_JOB_TIMEOUT_MINUTES
    assert job["timeout-minutes"] * 60 > (
        STAND_PROVISIONING_TIMEOUT_SECONDS + MATRIX_RUNNER_TIMEOUT_SECONDS
    )
    assert _workflow()["jobs"]["cleanup"]["timeout-minutes"] == STAND_CLEANUP_JOB_TIMEOUT_MINUTES


def test_make_targets_preserve_the_canonical_suite_contract():
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert 'test-live-mega-noop:\n\t@echo "Running mega-noop' in makefile
    assert "pytest tests/live/test_full_pipeline.py::TestFullPipeline -v" in makefile
    assert "test-live-mega: test-live-mega-noop" in makefile
    assert 'test-live-mega-llm:\n\t@echo "Running mega-llm' in makefile
    assert "pytest tests/live/test_full_pipeline.py::TestFullPipelineLLM -v" in makefile
    assert "test-live-matrix:\n\t@$(MAKE) --no-print-directory stand-run SUITE=matrix" in makefile
    assert "# Legacy aggregate, not a named suite:" in makefile


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


def test_retention_is_bounded_and_covers_every_run_owned_resource():
    """Debug retention may exist; outliving the run that owns it may not.

    Removing the input outright was one answer to "retained runs leak DNS", but
    it also removed the only way to inspect a failure on the machines that
    produced it — which is how the contour was debugged at all. The property that
    actually matters is narrower: whatever retention holds, the sweep takes, and
    the machines and their name are held and released together. A name left
    pointing at an address the provider reassigns is the leak; a pair kept for an
    hour under a tag the sweep knows is not.
    """
    workflow = WORKFLOW.read_text()

    cleanup = _cleanup_steps()
    machine_cleanup = cleanup["Cleanup and observe run-tagged machines"]
    dns_cleanup = cleanup["Remove the run's DNS record"]
    assert machine_cleanup["if"] == dns_cleanup["if"], (
        "machines and their DNS record must be released together, or retention "
        "keeps one and drops the other"
    )
    assert "keep_machines" in machine_cleanup["if"]

    sweep = _sweep_steps()["Sweep expired run-tagged machines"]["run"]
    assert "stand_lifecycle sweep --ttl-hours" in sweep
    assert "stand_dns sweep --ttl-hours" in sweep, (
        "the sweep that bounds retention must take the record as well as the machines"
    )
    assert "keep_machines" in workflow


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
    control_plane = CONTROL_PLANE_PLAYBOOK.read_text()

    assert "Install pinned uv for stand runner" in control_plane
    assert "uv --version" in steps["Bootstrap dynamic orchestrator"]["run"]
    assert run["if"] == "success()"
    assert "remote-invocation.log" in run["run"]
    assert ">/dev/null 2>&1" not in run["run"]


def test_control_plane_bootstrap_is_minimal_and_keeps_target_provisioning_separate():
    """The disposable control plane is not a deploy target.

    Target-only hardening remains owned by the product provisioning path that
    runs after the stand has registered the separate target machine.
    """
    bootstrap = _steps()["Bootstrap dynamic orchestrator"]
    bootstrap_run = bootstrap["run"]
    control_plane = CONTROL_PLANE_PLAYBOOK.read_text()
    target_provision = (
        WORKFLOW.parents[2] / "services/infra-service/ansible/playbooks/provision_software.yml"
    ).read_text()

    assert bootstrap["timeout-minutes"] == 15
    assert "provision_stand_control_plane.yml" in bootstrap_run
    assert "playbooks/bootstrap.yml" not in bootstrap_run
    assert "playbooks/provision_software.yml" not in bootstrap_run
    assert "ansible-galaxy collection install" not in bootstrap_run
    assert "ANSIBLE_PIPELINING=True" in bootstrap_run

    assert "gather_facts: false" in control_plane
    assert "Gather control plane facts" in control_plane
    assert "Wait for any possibly running apt/dpkg processes" in control_plane
    assert "upgrade: dist" not in control_plane
    assert "Create runtime user" in control_plane
    assert "docker-ce" in control_plane
    assert "docker compose version" in control_plane
    assert "docker buildx version" in control_plane
    assert "uv --version" in control_plane
    assert "Verify runtime user identity" in control_plane
    assert "/opt/codegen_orchestrator" in control_plane
    for target_only in (
        "name: deploy_target",
        "name: qa_identity",
        "name: monitoring",
        "ufw:",
        "timezone:",
    ):
        assert target_only not in control_plane

    for preserved_target_work in (
        "Upgrade all packages",
        "upgrade: dist",
        "name: deploy_target",
        "name: qa_identity",
        "name: monitoring",
    ):
        assert preserved_target_work in target_provision

    # The root workflow connection is deliberate, while protected material is
    # still narrowed to the unprivileged runtime identity after bootstrap.
    bring_up = _steps()["Bring up dynamic orchestrator and wait for API"]["run"]
    assert "ansible_user=root" in bootstrap_run
    assert "install -d -m 0700 -o ${RUNTIME_UID} -g ${RUNTIME_GID} /opt/secrets" in bring_up
    assert "chmod 0400 /opt/secrets/github_app.pem" in bring_up


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
