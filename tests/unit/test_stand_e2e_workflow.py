"""The e2e button: what it must not get wrong.

It exists so a suite can be started without an SSH key at hand. The properties
below are the ones whose absence is discovered at the worst moment — a second
run trampling the first, or a failed run whose logs were never collected.
"""

import ast
import base64
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time

import pytest
import yaml

from scripts.stand_acceptance import PROTECTED_STAND_SECRET_NAMES
from scripts.stand_credentials import CLAUDE_MINIMUM_TTL, validate_precreate_credentials
from scripts.stand_run import (
    LIVE_RUNNER_TIMEOUT_SECONDS,
    STAND_CLEANUP_JOB_TIMEOUT_MINUTES,
    STAND_JOB_RESERVE_SECONDS,
    STAND_JOB_TIMEOUT_MINUTES,
    STAND_PROVISIONING_TIMEOUT_SECONDS,
    STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS,
    SUITES,
)
from scripts.stand_telethon_preflight import needs_session
from scripts.wait_stand_provisioning import (
    DEFAULT_TIMEOUT_SECONDS as WAIT_STAND_PROVISIONING_TIMEOUT_SECONDS,
)
from shared.ssh_keys import normalize_admin_private_key

WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "stand-e2e.yml"
COLLECTOR = Path(__file__).parents[2] / "scripts" / "stand_collect_run_evidence.sh"
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

    assert set(options) == {
        "mega-noop",
        "mega-live",
        "mega-brief",
        "mega-brief-package",
        "custom",
    }
    assert "custom" in options, "an e2e invented later must be startable without a code change"
    # The retired paid route is not offered under any spelling.
    assert not {"mega-llm", "matrix", "llm"} & set(options)


def test_workflow_suite_names_match_the_runner_canonical_suite_table():
    options = _workflow()["on"]["workflow_dispatch"]["inputs"]["suite"]["options"]

    assert set(options) - {"custom"} == set(SUITES)
    assert _workflow()["on"]["workflow_dispatch"]["inputs"]["suite"]["default"] == "mega-noop"
    assert "inputs.suite" in _workflow()["run-name"]


def test_worker_and_qa_inputs_describe_when_the_runner_uses_them():
    inputs = _workflow()["on"]["workflow_dispatch"]["inputs"]

    for name in ("worker", "qa"):
        description = inputs[name]["description"]
        assert "mega-live" in description
        assert "mega-brief" in description
        assert "mega-noop" in description
        assert "mega-llm" not in description
        assert "matrix" not in description


def test_template_override_inputs_are_optional_and_documented():
    """The stand can scaffold from a candidate template; the production pin is untouched."""
    inputs = _workflow()["on"]["workflow_dispatch"]["inputs"]

    for name in ("template_source", "template_ref"):
        assert inputs[name]["type"] == "string"
        assert inputs[name]["required"] is False
        assert inputs[name]["default"] == ""
        assert "both or neither" in inputs[name]["description"]
        assert "production pin is untouched" in inputs[name]["description"]


def test_a_half_set_template_override_is_refused_before_anything_runs():
    steps = list(_steps())
    resolve = _steps()["Resolve the suite"]

    assert resolve["env"]["TEMPLATE_SOURCE"] == "${{ inputs.template_source }}"
    assert resolve["env"]["TEMPLATE_REF"] == "${{ inputs.template_ref }}"
    assert "both or neither" in resolve["run"]
    assert steps.index("Resolve the suite") < steps.index("Preflight ephemeral machines")


def test_a_template_override_reaches_the_suite_and_the_seeded_stand_configuration():
    steps = list(_steps())
    run = _steps()["Run selected stand suite"]
    override = _steps()["Override the stand template configuration"]

    assert run["env"]["TEMPLATE_SOURCE"] == "${{ inputs.template_source }}"
    assert run["env"]["TEMPLATE_REF"] == "${{ inputs.template_ref }}"
    assert "LIVE_TEMPLATE_REPO" in run["run"]
    assert "LIVE_TEMPLATE_REF" in run["run"]
    assert "scheduler.service_template_source" in override["run"]
    assert "scheduler.service_template_ref" in override["run"]
    assert steps.index("Bring up dynamic orchestrator and wait for API") < steps.index(
        "Override the stand template configuration"
    )
    assert steps.index("Override the stand template configuration") < steps.index(
        "Run selected stand suite"
    )


def test_stand_recreates_and_waits_for_scheduler_health_after_seeding():
    bring_up = _steps()["Bring up dynamic orchestrator and wait for API"]["run"]

    gate = "up -d --force-recreate --no-deps --wait --wait-timeout 180"
    assert gate in bring_up
    assert "scheduler-pipeline scheduler-infrastructure scheduler-maintenance" in bring_up
    assert bring_up.index("seed_agent_configs.py") < bring_up.index(gate)
    assert "grep -q system_configs_validated" not in bring_up


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
    """Every suite is one cell: SSH retries are never spent probing other pairs' logs."""
    collect = _steps()["Record machine manifest"]
    script = collect["run"]

    assert collect["env"]["SUITE"] == "${{ steps.suite.outputs.value }}"
    assert collect["env"]["WORKER"] == "${{ inputs.worker }}"
    assert collect["env"]["QA"] == "${{ inputs.qa }}"
    assert "matrix" not in script
    assert 'reports=(junit.xml report.tsv run.log "${QA}-${WORKER}.log")' in script
    assert 'for name in "${reports[@]}"' in script


def test_the_product_bot_token_reaches_both_level1_suites_and_no_other():
    """`mega-live` deploys the same Telegram-bot product `mega-noop` does."""
    script = _steps()["Run selected stand suite"]["run"]

    assert 'if [ "${SUITE}" = "mega-noop" ] || [ "${SUITE}" = "mega-live" ]; then' in script
    assert script.count('bot_token="${STAND_PRODUCT_BOT_TOKEN}"') == 1


def test_worker_failure_evidence_is_copied_before_the_ephemeral_host_is_deleted():
    collect = _steps()["Record machine manifest"]["run"]

    # The collection itself lives in one executable file, exercised below, and
    # the step streams it to the stand host rather than inlining a second copy.
    assert "bash -s /root/e2e-runs/latest" in collect
    assert "< scripts/stand_collect_run_evidence.sh" in collect
    assert "run-evidence-*.json" in COLLECTOR.read_text(encoding="utf-8")
    assert 'tar -C "${run_dir}" -xf -' in collect


def test_provisioning_failure_evidence_survives_a_pre_pytest_failure():
    provision = _steps()["Register and provision dynamic target"]["run"]
    collect = _steps()["Record machine manifest"]["run"]

    assert "provisioning-state.jsonl" in provision
    assert "provisioning-services.log" in provision
    assert "docker compose" in provision
    assert "infra-service scheduler-infrastructure" in provision
    assert "redact_diagnostic" in provision
    assert "provisioning-state.jsonl" in collect
    assert "provisioning-services.log" in collect
    assert "if [ -d /root/e2e-runs/latest ]" in collect


def _provisioning_remote_script() -> list[str]:
    """The part of the provisioning step that runs on the stand host."""
    provision = _steps()["Register and provision dynamic target"]["run"]
    remote = provision.split('root@"${PROD_HOST}" \'\n', maxsplit=1)[1].rsplit("\n'", maxsplit=1)[0]
    return remote.splitlines()


def test_the_provisioning_service_tails_are_kept_when_provisioning_succeeds():
    """ "Did the qa_identity role run" is a question about a *successful* run.

    Run 33718999040 recorded a target complete and QA then found its account
    had no `authorized_keys`; infra-service, which logs each play recap and the
    report closing the software phase, was collected only when provisioning
    failed, so the artifact could not answer it. The tail is now taken either
    way, through the same helper and the same protected-name allow-list, and a
    redaction path that did not complete publishes its own reason rather than
    the input it could not redact.
    """
    lines = _provisioning_remote_script()
    collection = next(
        index
        for index, line in enumerate(lines)
        if "logs --no-color --tail 300 infra-service scheduler-infrastructure" in line
    )
    guard = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == 'if [ "${wait_status}" -ne 0 ]; then'
    )

    assert collection < guard, "the tail must be collected before the step leaves on a failure"
    # Nothing but leaving is left in the failure branch, so success and failure
    # reach the collection by the same path.
    assert [line.strip() for line in lines[guard + 1 : guard + 3]] == [
        'exit "${wait_status}"',
        "fi",
    ]
    script = "\n".join(lines)
    assert "redact_diagnostic" in script
    assert "provisioning-services.log" in script
    assert "the redaction path did not complete" in script


def test_suite_failure_captures_every_service_that_carries_the_pipeline():
    """QA and deploy are stages of the pipeline, so their services are tails too."""
    collect = _steps()["Record machine manifest"]["run"]

    assert "logs --no-color --tail 300" in collect
    assert "scheduler-pipeline scheduler-infrastructure scheduler-maintenance" in collect
    assert "engineering-worker worker-manager worker-broker api" in collect
    assert "qa-worker deploy-worker" in collect
    # One redaction path on this side: the service tails. The target-host
    # snapshot is redacted by the suite that takes it, through the same helper.
    assert collect.count("from shared.diagnostics import redact_diagnostic") == 1
    assert "suite-services.log" in collect


def test_the_target_snapshot_travels_from_the_suite_not_an_ssh_after_teardown():
    """The runner cannot photograph containers the suite's own teardown removed."""
    collect = _steps()["Record machine manifest"]
    script = collect["run"]

    # It travels with the run evidence the suite wrote, out of the runner
    # directory, through the collector this step streams to the stand host.
    assert "< scripts/stand_collect_run_evidence.sh" in script
    assert "target-app.log" in COLLECTOR.read_text(encoding="utf-8")
    # And nothing here ssh's to the target host after the suite has ended.
    assert "TARGET_IP" not in script
    assert "TARGET_IP" not in (collect.get("env") or {})
    # The artifact still decides, through the one predicate the admission uses.
    assert (
        'python3 -m scripts.stand_acceptance needs-target-snapshot --run-dir "${run_dir}"' in script
    )


def _collect(files: dict[str, str] | None) -> set[str]:
    """Run the collection the way the workflow runs it, over one directory shape.

    `files` is what the run left in the runner directory; `None` is a run that
    never created it. The pipeline below is the workflow's own — the collector
    under `set -euo pipefail`, its output extracted by a local `tar` — so a
    `tar` that exits nonzero on a missing operand fails this call, which a text
    assertion about the same line cannot notice.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "latest"
        if files is not None:
            source.mkdir()
            for name, body in files.items():
                (source / name).write_text(body, encoding="utf-8")
        extracted = root / "run"
        extracted.mkdir()
        subprocess.run(
            [
                "bash",
                "-c",
                'set -euo pipefail; bash "$1" "$2" | tar -C "$3" -xf -',
                "collect-run-evidence",
                str(COLLECTOR),
                str(source),
                str(extracted),
            ],
            check=True,
        )
        return {path.name for path in extracted.iterdir()}


def test_a_run_that_owes_no_target_snapshot_still_hands_over_its_evidence():
    """The absent optional file is a named absence, never a failed collection.

    This is the shape of every green run and of a failing run whose evidence
    asks for no snapshot. A collection that aborts here takes the service tails
    and both stated-reason fallbacks with it, silently.
    """
    collected = _collect(
        {
            "run-evidence-claude-claude.json": "{}",
            "debug-engineering.md": "dump",
            "junit.xml": "<testsuite/>",
        }
    )

    assert collected == {"run-evidence-claude-claude.json", "debug-engineering.md"}


def test_a_snapshot_the_suite_took_travels_with_the_run_evidence():
    collected = _collect(
        {
            "run-evidence-claude-claude.json": "{}",
            "debug-engineering.md": "dump",
            "target-app.log": "== containers ==",
        }
    )

    assert collected == {
        "run-evidence-claude-claude.json",
        "debug-engineering.md",
        "target-app.log",
    }


def test_a_snapshot_that_was_wanted_and_failed_leaves_the_rest_collectable():
    """The suite wanted one and wrote none: the path that most needs the tails."""
    collected = _collect({"run-evidence-claude-claude.json": "{}"})

    assert collected == {"run-evidence-claude-claude.json"}


def test_a_run_directory_with_nothing_in_it_is_an_empty_handover_not_a_failure():
    assert _collect({}) == set()


def test_a_run_that_never_created_its_directory_is_an_empty_handover_too():
    assert _collect(None) == set()


def test_a_target_snapshot_that_could_not_be_taken_is_named_not_absent():
    script = _steps()["Record machine manifest"]["run"]

    assert 'if [ "${target_required}" = "yes" ] && [ ! -f "${run_dir}/target-app.log" ]; then' in (
        script
    )
    assert "the target host snapshot is unavailable: ${target_attempt}" in script
    assert "none reached the runner directory" in script


def test_stand_target_uses_the_typed_fast_profile_and_real_immediate_health_probe():
    provision = _steps()["Register and provision dynamic target"]["run"]

    assert "--profile stand_e2e" in provision
    assert "--no-require-fresh-metrics" in provision
    assert "scheduler-infrastructure python -m src.stand_health_probe" in provision
    assert "first_health_ready" in provision
    assert (
        provision.index("--no-require-fresh-metrics")
        < provision.index("scheduler-infrastructure python -m src.stand_health_probe")
        < provision.rindex("scripts.wait_stand_provisioning")
    )


def test_remote_target_provisioning_script_does_not_break_its_ssh_quote():
    provision = _steps()["Register and provision dynamic target"]["run"]
    remote_script = provision.split('root@"${PROD_HOST}" \'\n', maxsplit=1)[1].rsplit(
        "\n'", maxsplit=1
    )[0]

    assert "'" not in remote_script


def test_the_live_runner_path_fits_in_the_job_timeout():
    """The provisioned stand, `mega-live`'s runner path and the job reserve fit."""
    job = _workflow()["jobs"]["e2e"]

    assert job["timeout-minutes"] == STAND_JOB_TIMEOUT_MINUTES == 360
    assert job["timeout-minutes"] * 60 >= (
        STAND_PROVISIONING_TIMEOUT_SECONDS
        + STAND_WORKFLOW_PREPROVISION_RESERVE_SECONDS
        + LIVE_RUNNER_TIMEOUT_SECONDS
        + STAND_JOB_RESERVE_SECONDS
    )
    assert _workflow()["jobs"]["cleanup"]["timeout-minutes"] == STAND_CLEANUP_JOB_TIMEOUT_MINUTES


def test_make_targets_preserve_the_canonical_suite_contract():
    makefile = MAKEFILE.read_text(encoding="utf-8")

    assert 'test-live-mega-noop:\n\t@echo "Running mega-noop' in makefile
    assert "pytest tests/live/test_full_pipeline.py::TestFullPipeline -v" in makefile
    assert "test-live-mega: test-live-mega-noop" in makefile
    # Level 1 is told of no developer, whatever the caller's environment carries.
    assert "env -u LIVE_WORKER_AGENT_TYPE -u LIVE_LLM_QA -u LIVE_QA_AGENT_TYPE" in makefile
    assert "test-live-mega-live:\n\t@$(MAKE) --no-print-directory stand-run SUITE=mega-live" in (
        makefile
    )
    assert "TestFullPipelineLLM" not in makefile
    assert "mega-llm" not in makefile
    assert 'test-live-mega-brief:\n\t@echo "Running mega-brief' in makefile
    assert (
        "pytest tests/live/test_product_brief_pipeline.py::TestProductBriefPipeline -v" in makefile
    )
    assert "test-live-matrix" not in makefile and "SUITE=matrix" not in makefile
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


def test_exact_worker_release_is_validated_before_any_provider_or_dns_action():
    steps = list(_steps())
    gate = _steps()["Validate exact worker image release"]

    assert gate["env"]["WORKER_IMAGE_TAG"] == "${{ github.sha }}"
    assert gate["env"]["GHCR_TOKEN"] == "${{ secrets.GHCR_TOKEN || github.token }}"  # noqa: S105
    assert "RELEASE_VALIDATION_ONLY=true" not in gate["run"]
    assert gate["env"]["DIGEST_FILE"] == "${{ runner.temp }}/stand-precreate-worker-images.json"
    assert "pull-worker-images.sh" in gate["run"]
    assert "BITLAUNCH_API_KEY" not in gate.get("env", {})
    assert "stand_lifecycle" not in gate["run"]
    assert "stand_dns" not in gate["run"]
    assert steps.index("Validate exact worker image release") < steps.index(
        "Preflight ephemeral machines"
    )
    assert steps.index("Validate exact worker image release") < steps.index(
        "Create ephemeral machines"
    )
    assert steps.index("Validate exact worker image release") < steps.index(
        "Give the stand a resolvable name"
    )


def test_codex_auth_preflight_uses_the_verified_pinned_worker_before_machine_creation():
    """A refreshable session must fail on the runner, not after billed provisioning."""
    steps = list(_steps())
    release = _steps()["Validate exact worker image release"]
    restore = _steps()["Restore refreshable Codex auth profile"]
    auth = _steps()["Authenticate Codex against exact worker image"]
    preflight_persist = _steps()["Persist preflight-refreshed Codex auth profile"]
    persist = _steps()["Persist refreshed Codex auth profile"]

    assert "RELEASE_VALIDATION_ONLY=true" not in release["run"]
    assert "pull-worker-images.sh" in release["run"]
    assert restore["env"]["CODEX_AUTH_JSON"] == "${{ secrets.CODEX_AUTH_JSON }}"
    assert "CODEX_ACCESS_TOKEN" not in restore["env"]
    assert "CODEX_ACCESS_TOKEN" not in auth.get("env", {})
    assert "auth.json" in restore["run"]
    assert "refresh_token" in restore["run"]
    assert "CODEX_AUTH_JSON" not in auth.get("env", {})
    assert "worker-base-codex:latest" in auth["run"]
    assert '"--entrypoint", "codex"' in auth["run"]
    assert "--mount" in auth["run"]
    assert "auth.json" in auth["run"]
    assert "codex exec" in auth["run"]
    assert '"--entrypoint", "id", image, "-u"' in auth["run"]
    assert '"--entrypoint", "id", image, "-g"' in auth["run"]
    assert 'output.write(f"worker_uid={worker_uid}' in auth["run"]
    assert '"sudo", "chown", "-R"' in auth["run"]
    assert "finally:" in auth["run"]
    assert "os.getuid()" in auth["run"]
    assert "stdout=subprocess.DEVNULL" in auth["run"]
    assert "stderr=subprocess.DEVNULL" in auth["run"]
    assert "codex_auth_not_confirmed" in auth["run"]
    assert "codex_auth_rejected" not in auth["run"]
    assert "codex_auth_unavailable" in auth["run"]
    assert "codex_cli_mismatch" in auth["run"]
    assert "--rm" in auth["run"]
    assert 'output.write(f"worker_uid={worker_uid}\\nworker_gid={worker_gid}\\n")' in auth["run"]
    assert "GITHUB_STEP_SUMMARY" not in auth["run"]
    assert "tee " not in auth["run"]
    assert preflight_persist["if"] == "success()"
    assert "gh secret set CODEX_AUTH_JSON --env stand" in preflight_persist["run"]
    assert persist["if"] == "${{ always() && steps.codex-auth-preflight.outcome == 'success' }}"
    assert persist["env"]["GH_TOKEN"] == "${{ secrets.STAND_GITHUB_SECRETS_WRITE_TOKEN }}"  # noqa: S105
    assert "gh secret set CODEX_AUTH_JSON --env stand" in persist["run"]
    assert 'remote_auth="${RUNNER_TEMP}/stand-codex-refreshed-auth.json"' in persist["run"]
    assert '< "${remote_auth}"' in persist["run"]
    assert 'mv "${remote_auth}" "${auth_path}"' not in persist["run"]
    assert "stand-codex-refreshed-auth-valid" in persist["run"]
    assert "stand-codex-remote-profile-installed" in persist["run"]
    assert "codex_auth_persist_remote_unavailable" not in persist["run"]
    assert "upload-artifact" not in persist["run"]
    assert (
        steps.index("Authenticate Codex against exact worker image")
        < steps.index("Persist preflight-refreshed Codex auth profile")
        < steps.index("Preflight ephemeral machines")
    )
    assert steps.index("Authenticate Codex against exact worker image") < steps.index(
        "Preflight ephemeral machines"
    )
    assert steps.index("Authenticate Codex against exact worker image") < steps.index(
        "Create ephemeral machines"
    )
    assert steps.index("Authenticate Codex against exact worker image") < steps.index(
        "Give the stand a resolvable name"
    )


def test_codex_profile_tokens_are_redaction_needles_in_the_handoff_and_attested_for_cleanup():
    handoff = _steps()["Admit cleanup handoff"]
    final = _cleanup_steps()["Admit final artifact"]

    assert "--protected-profile" in handoff["run"]
    assert "stand-codex-initial-auth.json" in handoff["run"]
    assert "stand-codex-profile/auth.json" in handoff["run"]
    assert "stand-codex-refreshed-auth.json" in handoff["run"]
    assert "stand-codex-refreshed-auth-valid" in handoff["run"]
    assert "--profile-attestation" in handoff["run"]
    assert "--require-profile-attestation" in final["run"]
    assert "CODEX_AUTH_JSON" not in final.get("env", {})


def test_stand_profile_is_owned_by_the_exact_worker_image_identity():
    bootstrap = _steps()["Bring up dynamic orchestrator and wait for API"]

    assert (
        bootstrap["env"]["CODEX_WORKER_UID"]
        == "${{ steps.codex-auth-preflight.outputs.worker_uid }}"
    )
    assert (
        bootstrap["env"]["CODEX_WORKER_GID"]
        == "${{ steps.codex-auth-preflight.outputs.worker_gid }}"
    )
    assert "install -d -m 0700 -o ${CODEX_WORKER_UID} -g ${CODEX_WORKER_GID}" in bootstrap["run"]
    assert (
        "chown ${CODEX_WORKER_UID}:${CODEX_WORKER_GID} /opt/secrets/stand-codex" in bootstrap["run"]
    )


def test_missing_exact_worker_release_fails_with_retry_guidance_not_a_local_build():
    job = _workflow()["jobs"]["e2e"]
    # The stand's worker pull starts in the background after bootstrap and is joined
    # before the suite; the properties hold across the two steps.
    start = _steps()["Start the stand's release pulls and uv environment"]
    step = _steps()["Join the worker image release pull"]
    gate = _steps()["Validate exact worker image release"]

    assert job["permissions"]["packages"] == "read"
    assert job["steps"][0]["with"]["fetch-depth"] == 0
    assert "${GITHUB_SHA}" in start["run"]
    assert "${GITHUB_SHA}" in step["run"]
    for script in (start["run"], step["run"]):
        assert "git merge-base HEAD origin/main" not in script
        assert "GHCR_TOKEN='${GHCR_TOKEN}'" not in script
        assert "ensure-worker-images" not in script
    assert "read -r GHCR_TOKEN" in start["run"]
    assert 'case "${pulled}"' not in step["run"]
    assert "FATAL: pulling the worker image release failed" in step["run"]
    assert "release_not_published" in gate["run"]
    assert "Retry after the post-merge worker-image publication CI completes" in gate["run"]


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

    assert "python -m scripts.stand_run" in run["run"]
    assert "python scripts/stand_run.py" not in run["run"]
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
    assert 'tee "${RUNNER_TEMP}/remote-invocation.log"' in run["run"]
    assert "2>&1 | tee" in run["run"]
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
    assert "Settle first boot before apt or Docker work" in control_plane
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


def test_control_plane_apt_operations_tolerate_a_late_lock_with_a_bounded_wait():
    """A lock acquired after the initial probe must not fail Docker installation.

    The apt module owns retries for its lock only; a task-level retry would
    repeat unrelated repository or package failures and make them ambiguous.
    """
    [play] = yaml.safe_load(CONTROL_PLANE_PLAYBOOK.read_text())
    timeout = play["vars"]["stand_apt_lock_timeout_seconds"]
    apt_tasks = {
        task["name"]: task["ansible.builtin.apt"]
        for task in [*play["pre_tasks"], *play["tasks"]]
        if "ansible.builtin.apt" in task
    }
    docker_repository = next(
        task for task in play["tasks"] if task["name"] == "Add Docker apt repository"
    )

    assert timeout == 300
    assert set(apt_tasks) == {
        "Update apt cache without changing the base image",
        "Install control-plane host tools",
        "Install Docker Engine and compose tooling",
    }
    assert all(
        task["lock_timeout"] == "{{ stand_apt_lock_timeout_seconds }}"
        for task in apt_tasks.values()
    )
    assert not any("retries" in task or "until" in task for task in apt_tasks.values())
    assert docker_repository["ansible.builtin.apt_repository"]["update_cache"] is False
    assert apt_tasks["Install Docker Engine and compose tooling"]["update_cache"] is True


def _first_boot_settle_tasks() -> tuple[dict, list[dict]]:
    [play] = yaml.safe_load(CONTROL_PLANE_PLAYBOOK.read_text())
    return play, play["pre_tasks"]


def test_first_boot_is_settled_before_any_apt_or_docker_work_and_survives_one_drop():
    """Runs 35594323906/35597245917/35595495097 lost the host to its own first boot.

    The image's automatic upgrades are stopped for good on this disposable VM,
    cloud-init is waited for, and a drop or reboot during that window gets one
    bounded reconnect before the same settle runs again, this time strictly.
    """
    play, pre_tasks = _first_boot_settle_tasks()
    names = [task["name"] for task in pre_tasks]
    first, reconnect, again = (
        pre_tasks[names.index("Settle first boot before apt or Docker work")],
        pre_tasks[names.index("Wait for control plane to return after a first-boot drop")],
        pre_tasks[names.index("Settle first boot again after reconnecting")],
    )
    all_tasks = [*pre_tasks, *play["tasks"]]
    first_apt_or_package_work = min(
        index
        for index, task in enumerate(all_tasks)
        if any(
            key.startswith("ansible.builtin.apt") or key == "ansible.builtin.get_url"
            for key in task
        )
    )

    assert names.index("Wait for control plane to be reachable") == 0
    assert (
        names.index(first["name"])
        < names.index(reconnect["name"])
        < names.index(again["name"])
        < names.index("Gather control plane facts")
        < first_apt_or_package_work
    )
    assert first["ignore_unreachable"] is True
    assert first["register"] == "stand_first_boot_settle"
    assert reconnect["when"] == again["when"] == "stand_first_boot_settle is unreachable"
    assert reconnect["ansible.builtin.wait_for_connection"]["timeout"] == (
        "{{ stand_first_boot_reconnect_timeout_seconds }}"
    )
    assert "ignore_unreachable" not in again
    assert again["ansible.builtin.shell"] == first["ansible.builtin.shell"]
    assert play["vars"]["stand_first_boot_reconnect_timeout_seconds"] == 180
    assert play["vars"]["stand_first_boot_settle_timeout_seconds"] == 300
    # The whole bootstrap step has 15 minutes: two settles plus a reconnect fit.
    assert (
        2 * play["vars"]["stand_first_boot_settle_timeout_seconds"]
        + play["vars"]["stand_first_boot_reconnect_timeout_seconds"]
    ) < 15 * 60
    assert "ServerAliveInterval=" in play["vars"]["ansible_ssh_extra_args"]
    assert "Wait for any possibly running apt/dpkg processes" not in names


def _run_settle(tmp: Path, *, timeout_seconds: int, cloud_init_rc: int, lock_polls: int):
    _, pre_tasks = _first_boot_settle_tasks()
    [settle] = [
        task for task in pre_tasks if task["name"] == "Settle first boot before apt or Docker work"
    ]
    command = settle["ansible.builtin.shell"]["cmd"].replace(
        "{{ stand_first_boot_settle_timeout_seconds }}", str(timeout_seconds)
    )
    assert "{{" not in command and "{%" not in command and "{#" not in command
    calls = tmp / "calls"
    stubs = tmp / "bin"
    stubs.mkdir()
    for name, body in {
        "systemctl": "exit 0",
        "cloud-init": f'[ "$1" = status ] && [ "$2" = --wait ] && exit {cloud_init_rc}; exit 0',
        # Held for the first `lock_polls` probes, then free; negative = held forever.
        "fuser": (
            f'n=$(cat "{tmp}/polls" 2>/dev/null || echo 0); echo $((n + 1)) > "{tmp}/polls"; '
            f'[ {lock_polls} -lt 0 ] || [ "$n" -lt {lock_polls} ]'
        ),
        "dpkg": "exit 0",
        "sleep": "exit 0",
    }.items():
        stub = stubs / name
        stub.write_text(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\n{body}\n')
        stub.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", "-c", command],
        env={"PATH": f"{stubs}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return result, recorded


def test_first_boot_settle_stops_upgrades_then_waits_for_cloud_init_then_locks():
    with tempfile.TemporaryDirectory() as tmp:
        result, calls = _run_settle(Path(tmp), timeout_seconds=20, cloud_init_rc=0, lock_polls=2)

    assert result.returncode == 0, result.stderr
    units = "apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service"
    assert calls[:3] == [
        f"systemctl mask {units}",
        f"systemctl stop {units}",
        "cloud-init status --wait",
    ]
    assert [call.split()[0] for call in calls[3:]] == [
        "fuser",
        "sleep",
        "fuser",
        "sleep",
        "fuser",
        "dpkg",
    ]
    assert calls[-1] == "dpkg --configure -a"


def test_first_boot_settle_accepts_a_finished_cloud_init_with_warnings_or_errors():
    for rc in (1, 2):
        with tempfile.TemporaryDirectory() as tmp:
            result, calls = _run_settle(
                Path(tmp), timeout_seconds=20, cloud_init_rc=rc, lock_polls=0
            )

        assert result.returncode == 0, result.stderr
        assert f"cloud-init finished with status {rc}" in result.stderr
        assert "cloud-init status --long" in calls
        assert calls[-1] == "dpkg --configure -a"


def test_first_boot_settle_fails_clearly_when_the_host_never_settles():
    with tempfile.TemporaryDirectory() as tmp:
        result, calls = _run_settle(Path(tmp), timeout_seconds=1, cloud_init_rc=0, lock_polls=-1)

    assert result.returncode == 124
    assert "waiting for dpkg/apt locks" in result.stderr
    assert "dpkg --configure -a" not in calls


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

    for name in ("STAND_CLAUDE_CODE_OAUTH_TOKEN", "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT"):
        assert name in step["env"]
        assert f'"{name}"' in render
    assert "STAND_CODEX_ACCESS_TOKEN" not in step["env"]
    assert step["env"]["HOST_CODEX_HOME"] == "/opt/secrets/stand-codex"
    assert step["env"]["HOST_CODEX_VALIDATION_PATH"] == "/host-codex"
    assert "HOST_CLAUDE_DIR" not in step["env"]


def test_stand_overlay_keeps_only_the_refreshable_codex_host_session_mount():
    overlay = (WORKFLOW.parents[2] / "docker-compose.stand.yml").read_text()

    assert 'HOST_CLAUDE_DIR: ""' in overlay
    assert "HOST_CODEX_HOME: ${HOST_CODEX_HOME}" in overlay
    assert "HOST_CODEX_VALIDATION_PATH: /host-codex" in overlay
    assert "/host-claude" not in overlay
    assert "${HOST_CODEX_HOME}:/host-codex:ro" in overlay


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


def _write_target_key(secret: str) -> str:
    """Run the registration step's own key writer and return the file it left.

    The script below is the step's `run` up to the point the material leaves the
    runner, so the writer under test is the workflow's line and not a copy of it.
    The step's cleanup trap shreds the file on exit, so the copy is taken inside
    the same shell.
    """
    script = _steps()["Register and provision dynamic target"]["run"]
    prefix = script.split('key_path="${RUNNER_TEMP}/stand-bootstrap.key"')[0]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        copy = root / "delivered.key"
        subprocess.run(
            ["bash", "-c", prefix + '\ncp "${target_key}" "$1"\n', "write-target-key", str(copy)],
            check=True,
            env={
                "PATH": os.environ["PATH"],
                "RUNNER_TEMP": str(root),
                "TARGET_ID": "6a920e74c9c98a452507b09b",
                "TARGET_IP": "203.0.113.19",
                "STAND_RUN_TAG": "gha-41-1",
                "SSH_PRIVATE_KEY": secret,
            },
        )
        return copy.read_text()


def test_the_registration_key_file_is_accepted_when_the_secret_lost_its_last_newline():
    """The refusal that killed stand run 35380550303, pinned at its producer.

    `SSH_PRIVATE_KEY` reaches the step as an environment variable, and a stored
    secret whose final newline was eaten is exactly the shape the API refuses
    with `ssh_key rejected: no_terminal_newline`. The writer restores it, so the
    key the script submits parses.
    """
    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp) / "fleet"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(generated)],
            check=True,
            stdin=subprocess.DEVNULL,
        )
        key_text = generated.read_text()

    delivered = _write_target_key(key_text.rstrip("\n"))

    assert delivered.endswith("\n")
    assert normalize_admin_private_key(delivered).fingerprint == (
        normalize_admin_private_key(key_text).fingerprint
    )


def test_a_secret_that_kept_its_newline_still_yields_one_accepted_key():
    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp) / "fleet"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(generated)],
            check=True,
            stdin=subprocess.DEVNULL,
        )
        key_text = generated.read_text()

    delivered = _write_target_key(key_text)

    assert normalize_admin_private_key(delivered).fingerprint == (
        normalize_admin_private_key(key_text).fingerprint
    )


def test_the_docker_steps_of_the_stand_have_their_own_bounds():
    """A hung pull fails its step, not the 360-minute job with two paid machines under it."""
    steps = _steps()

    # Bring-up now joins the background service release pull (bounded at 20 minutes)
    # and the third-party pull (10) instead of building; the worker release is joined
    # (bounded at 20) in its own step before the suite.
    assert steps["Start the stand's release pulls and uv environment"]["timeout-minutes"] == 5
    assert steps["Bring up dynamic orchestrator and wait for API"]["timeout-minutes"] == 30
    assert steps["Join the worker image release pull"]["timeout-minutes"] == 25
    # The provisioning wait keeps its own deadline and reports on it; the step bound
    # only catches what hangs outside the wait.
    target = steps["Register and provision dynamic target"]["timeout-minutes"]
    assert target == 30
    assert target * 60 > WAIT_STAND_PROVISIONING_TIMEOUT_SECONDS
    for step in steps.values():
        assert step.get("timeout-minutes", 0) < STAND_JOB_TIMEOUT_MINUTES


# --- the stand runs the tested release: pulled, never built ------------------------------
#
# The workflow cannot run in PR CI, so its shell is exercised here offline: each step's
# script is run against a fake `ssh` that records what would have run on the stand host,
# and that remote shell is then run against a fake `docker` in a scratch directory
# standing in for /opt/codegen_orchestrator.

START_STEP = "Start the stand's release pulls and uv environment"
BRING_UP_STEP = "Bring up dynamic orchestrator and wait for API"
WORKER_JOIN_STEP = "Join the worker image release pull"
RELEASE_WAIT_STEP = "Wait for this revision's worker and service releases"
TIMING_STEP = "Report stand bring-up timing"
BACKGROUND_SCRIPT = WORKFLOW.parents[2] / "scripts" / "stand_background.sh"
SERVICE_RELEASE_SCRIPT = WORKFLOW.parents[2] / "scripts" / "service_release.py"
STAND_SHA = "0123456789abcdef0123456789abcdef01234567"

FAKE_SSH = """#!/usr/bin/env bash
# The remote command is the last argument; stdin is what the runner piped to it.
printf '%s\\0' "${@: -1}" >> "${FAKE_SSH_COMMANDS}"
cat > "${FAKE_SSH_STDIN}.$(date +%s%N)" || true
"""

FAKE_DOCKER_FOR_STAND = """#!/usr/bin/env bash
echo "docker $*" >> "${FAKE_DOCKER_LOG}"
case "$*" in
    *"config --format json"*) cat "${FAKE_COMPOSE_CONFIG}" ;;
esac
exit 0
"""


def _job_env() -> dict[str, str]:
    return {key: str(value) for key, value in _workflow()["jobs"]["e2e"]["env"].items()}


def _joined(script: str) -> str:
    """A step script with its line continuations joined, as the shell reads it."""
    return script.replace("\\\n", " ")


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def _run_step_against_fake_ssh(tmp: Path, step: str, extra_env: dict[str, str]) -> list[str]:
    """Run one step's script on a fake runner; return the remote commands it sent."""
    binaries = tmp / "runner-bin"
    _write_executable(binaries / "ssh", FAKE_SSH)
    _write_executable(binaries / "scp", "#!/usr/bin/env bash\nexit 0\n")
    commands = tmp / "ssh-commands"
    runner_temp = tmp / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    environment = {
        "PATH": f"{binaries}:/usr/bin:/bin",
        "HOME": str(tmp),
        "RUNNER_TEMP": str(runner_temp),
        "SSH_OPTS": "-o BatchMode=yes",
        "PROD_HOST": "192.0.2.10",
        "GITHUB_SHA": STAND_SHA,
        "FAKE_SSH_COMMANDS": str(commands),
        "FAKE_SSH_STDIN": str(tmp / "ssh-stdin"),
        **_job_env(),
        **extra_env,
    }
    result = subprocess.run(
        ["bash", "-e", "-c", _steps()[step]["run"]],
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return [command for command in commands.read_text().split("\0") if command]


def _stand_host(tmp: Path) -> Path:
    """A scratch /opt/codegen_orchestrator with the real background and release helpers."""
    host = tmp / "host"
    (host / "scripts").mkdir(parents=True)
    (host / "scripts" / "stand_background.sh").write_text(BACKGROUND_SCRIPT.read_text())
    (host / "scripts" / "service_release.py").write_text(SERVICE_RELEASE_SCRIPT.read_text())
    return host


def _on_host(command: str, host: Path) -> str:
    return command.replace("/opt/codegen_orchestrator", str(host))


def test_the_release_gate_waits_boundedly_for_both_chains_before_any_money_is_spent():
    steps = list(_steps())
    gate = _steps()[RELEASE_WAIT_STEP]

    assert "python3 scripts/wait_release.py" in gate["run"]
    assert '--revision "${GITHUB_SHA}"' in gate["run"]
    assert "--chain worker --chain service" in _joined(gate["run"])
    assert "--timeout-seconds 600" in _joined(gate["run"])
    assert gate["timeout-minutes"] * 60 > 600
    assert "Retry after the post-merge CI" in gate["run"]
    assert gate["env"]["GITHUB_TOKEN"] == "${{ github.token }}"  # noqa: S105
    assert _workflow()["jobs"]["e2e"]["permissions"]["actions"] == "read"
    for paid in (
        "Preflight ephemeral machines",
        "Create ephemeral machines",
        "Give the stand a resolvable name",
    ):
        assert steps.index(RELEASE_WAIT_STEP) < steps.index(paid)


def test_no_step_of_the_workflow_builds_an_image():
    """The stand runs the tested release; a build there is what made it fragile."""
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            script = _joined(step.get("run", ""))
            assert not re.search(r"\bup\b[^\n]*(?<![\w-])--build\b", script), step.get("name")
            assert not re.search(r"compose\b[^\n]*\sbuild\b", script, re.IGNORECASE), step.get(
                "name"
            )
    bring_up = _joined(_steps()[BRING_UP_STEP]["run"])
    ups = re.findall(r"\$\{COMPOSE\} up [^\n]*", bring_up)
    assert len(ups) == 2
    for up in ups:
        assert "--no-build --pull never" in up
    assert "pull --ignore-buildable --policy missing" in bring_up


def test_background_work_starts_right_after_bootstrap_and_is_joined_before_its_consumer():
    steps = list(_steps())
    bring_up = _joined(_steps()[BRING_UP_STEP]["run"])
    suite = _steps()["Run selected stand suite"]["run"]

    assert steps.index(START_STEP) == steps.index("Bootstrap dynamic orchestrator") + 1
    # The service release is joined, and turned into the override, before `up`.
    join_service = bring_up.index('stand_background.sh join "${background}" service')
    override = bring_up.index("scripts/service_release.py compose-override")
    up = bring_up.index("up -d --remove-orphans --no-build --pull never")
    assert join_service < override < up
    assert bring_up.index('join "${background}" third-party') < up
    # The worker release is joined after provisioning, before the suite starts workers.
    assert (
        steps.index("Register and provision dynamic target")
        < steps.index(WORKER_JOIN_STEP)
        < steps.index("Run selected stand suite")
    )
    assert "Provide worker base images on the stand" not in steps
    # The suite joins its own environment, then runs frozen on it.
    assert suite.index("stand_background.sh join %q uv 600") < suite.index("uv run python")
    assert "export UV_FROZEN=1" in suite
    assert suite.index("export UV_FROZEN=1") < suite.index("uv run python")


def test_the_start_step_launches_three_detached_jobs_with_the_token_only_on_stdin(tmp_path):
    token = "ghcr-token-that-must-not-reach-a-command-line"  # noqa: S105
    background = tmp_path / "bg"
    commands = _run_step_against_fake_ssh(
        tmp_path,
        START_STEP,
        {"GHCR_TOKEN": token, "GHCR_OWNER": "test-owner", "STAND_BACKGROUND_DIR": str(background)},
    )
    assert len(commands) == 1
    remote = commands[0]
    assert token not in remote
    assert "IFS= read -r GHCR_TOKEN" in remote

    # Run what the stand would run, against fake pullers and a fake uv.
    host = _stand_host(tmp_path)
    for script, variables in (
        ("pull-service-images.sh", "tag=${SERVICE_IMAGE_TAG} digest=${DIGEST_FILE}"),
        (
            "pull-worker-images.sh",
            "tag=${WORKER_IMAGE_TAG} subset=${WORKER_IMAGE_SUBSET} digest=${DIGEST_FILE}",
        ),
    ):
        _write_executable(
            host / "infra" / "scripts" / script,
            f'#!/usr/bin/env bash\necho "{variables} owner=${{GHCR_OWNER}} '
            'token=${GHCR_TOKEN:-unset}"\n',
        )
    stand_bin = tmp_path / "stand-bin"
    _write_executable(
        stand_bin / "uv", '#!/usr/bin/env bash\necho "uv $* token=${GHCR_TOKEN:-unset}"\n'
    )
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", _on_host(remote, host)],
        input=token + "\n",
        capture_output=True,
        text=True,
        env={"PATH": f"{stand_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started < 5, "the start step must not wait for the jobs"

    logs = {}
    for job in ("service", "worker", "uv"):
        joined = subprocess.run(
            ["bash", str(BACKGROUND_SCRIPT), "join", str(background), job, "10"],
            capture_output=True,
            text=True,
            env={**os.environ, "STAND_BACKGROUND_POLL_SECONDS": "0.05"},
            timeout=30,
        )
        assert joined.returncode == 0, joined.stderr
        logs[job] = (background / f"{job}.log").read_text()

    assert f"tag={STAND_SHA}" in logs["service"]
    assert f"digest={host}/deployed-service-images.json" in logs["service"]
    assert f"token={token}" in logs["service"]
    assert f"tag={STAND_SHA}" in logs["worker"]
    assert f"subset={_job_env()['STAND_WORKER_IMAGES']}" in logs["worker"]
    assert f"digest={host}/deployed-worker-images.json" in logs["worker"]
    assert "uv sync --frozen token=unset" in logs["uv"], "the uv job never sees the token"


def test_the_stand_pulls_exactly_the_worker_images_its_suites_run():
    images = _job_env()["STAND_WORKER_IMAGES"].split()

    assert images == ["worker-base-common", "worker-base-claude", "worker-base-codex"]
    assert "worker-base-factory" not in images


def _bring_up_remote(tmp: Path, background: Path) -> str:
    runner_temp = tmp / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    (runner_temp / "stand-bootstrap-started").write_text("1000\n")
    commands = _run_step_against_fake_ssh(
        tmp,
        BRING_UP_STEP,
        {
            "STAND_BACKGROUND_DIR": str(background),
            "RUNTIME_UID": "1001",
            "RUNTIME_GID": "1001",
            "CODEX_WORKER_UID": "1002",
            "CODEX_WORKER_GID": "1002",
        },
    )
    main = [command for command in commands if "stand_background.sh join" in command]
    assert len(main) == 1
    return main[0]


def _finished_job(background: Path, job: str, status: int, log: str = "") -> None:
    background.mkdir(exist_ok=True)
    (background / f"{job}.started").write_text("1000\n")
    (background / f"{job}.finished").write_text("1090\n")
    (background / f"{job}.log").write_text(log)
    (background / f"{job}.status").write_text(f"{status}\n")


def _run_bring_up_on_stand(
    tmp: Path, remote: str, host: Path
) -> tuple[subprocess.CompletedProcess, list[str]]:
    stand_bin = tmp / "stand-bin"
    _write_executable(stand_bin / "docker", FAKE_DOCKER_FOR_STAND)
    config = tmp / "compose-config.json"
    config.write_text(
        json.dumps(
            {
                "services": {
                    "api": {"build": {"context": "."}, "image": "codegen-orchestrator/api:local"},
                    "db": {"image": "pgvector/pgvector:0.8.6-pg16"},
                }
            }
        )
    )
    docker_log = tmp / "docker.log"
    docker_log.write_text("")
    result = subprocess.run(
        ["bash", "-c", _on_host(remote, host)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": f"{stand_bin}:/usr/bin:/bin",
            "HOME": str(tmp),
            "FAKE_DOCKER_LOG": str(docker_log),
            "FAKE_COMPOSE_CONFIG": str(config),
            "STAND_BACKGROUND_POLL_SECONDS": "0.05",
        },
        timeout=60,
    )
    return result, docker_log.read_text().splitlines()


def test_bring_up_runs_the_pulled_release_by_digest_and_builds_nothing(tmp_path):
    background = tmp_path / "bg"
    remote = _bring_up_remote(tmp_path, background)
    host = _stand_host(tmp_path)
    digest = "ghcr.io/test-owner/codegen-orchestrator/api@sha256:" + "a" * 64
    (host / "deployed-service-images.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "git_sha": STAND_SHA,
                "source_hash": "feedface",
                "images": {"api": {"reference": digest}},
            }
        )
    )
    _finished_job(background, "service", 0, "service images ready\n")

    result, calls = _run_bring_up_on_stand(tmp_path, remote, host)

    assert result.returncode == 0, result.stderr
    override = _job_env()["STAND_SERVICE_RELEASE_COMPOSE"]
    assert json.dumps(digest) in (host / override).read_text()
    assert not (background / "compose.json").exists(), "the resolved config is not left behind"
    pulls = [call for call in calls if " pull --ignore-buildable" in call]
    assert len(pulls) == 1
    ups = [call for call in calls if " up " in call]
    assert len(ups) == 2
    for up in ups:
        assert f"-f {override}" in up
        assert "--no-build --pull never" in up
    assert "scheduler-pipeline scheduler-infrastructure scheduler-maintenance" in ups[1]
    assert calls.index(ups[0]) < calls.index(
        next(call for call in calls if "alembic upgrade head" in call)
    )
    for call in calls:
        assert not re.search(r"(?<![\w-])--build\b|\sbuild\b", call), call


def test_bring_up_fails_closed_on_a_failed_service_pull_before_anything_starts(tmp_path):
    background = tmp_path / "bg"
    remote = _bring_up_remote(tmp_path, background)
    host = _stand_host(tmp_path)
    _finished_job(background, "service", 10, "FATAL: the release marker is not a usable record\n")

    result, calls = _run_bring_up_on_stand(tmp_path, remote, host)

    assert result.returncode == 10
    assert "background job service failed with exit 10" in result.stderr
    assert "not a usable record" in result.stderr
    assert not [call for call in calls if " up " in call]


def test_bring_up_fails_closed_on_a_service_pull_that_never_started(tmp_path):
    background = tmp_path / "bg"
    remote = _bring_up_remote(tmp_path, background)
    host = _stand_host(tmp_path)

    result, calls = _run_bring_up_on_stand(tmp_path, remote, host)

    assert result.returncode == 125
    assert not [call for call in calls if " up " in call]


def test_the_worker_join_fails_the_step_with_the_pull_exit_code():
    join = _steps()[WORKER_JOIN_STEP]

    assert "stand_background.sh join '${STAND_BACKGROUND_DIR}' worker 1200" in join["run"]
    assert "FATAL: pulling the worker image release failed" in join["run"]
    assert 'exit "${pulled}"' in join["run"]
    assert join["timeout-minutes"] * 60 > 1200


def test_every_rendered_step_script_parses(tmp_path):
    """No step ships a shell syntax error to the one budgeted live run."""
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            script = step.get("run")
            if not script:
                continue
            rendered = re.sub(r"\$\{\{[^}]*\}\}", "rendered", script)
            result = subprocess.run(
                ["bash", "-n", "-c", rendered], capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, f"{step.get('name')}: {result.stderr}"


def test_the_remote_bring_up_script_parses(tmp_path):
    remote = _bring_up_remote(tmp_path, tmp_path / "bg")

    result = subprocess.run(["bash", "-n", "-c", remote], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_the_bring_up_span_and_the_pull_durations_are_reported():
    bootstrap = _steps()["Bootstrap dynamic orchestrator"]["run"]
    bring_up = _steps()[BRING_UP_STEP]["run"]
    timing = _steps()[TIMING_STEP]
    steps = list(_steps())

    assert bootstrap.splitlines()[2].strip() == (
        'date +%s > "${RUNNER_TEMP}/stand-bootstrap-started"'
    ), "the span starts with the Bootstrap step"
    assert bring_up.rstrip().endswith('(target 300s)"')
    assert 'echo "${healthy_epoch}" > "${RUNNER_TEMP}/stand-services-healthy"' in bring_up
    assert "$GITHUB_STEP_SUMMARY" in timing["run"]
    assert "stand_background.sh report" in timing["run"]
    for job in ("service", "worker", "uv", "third-party"):
        assert job in timing["run"].split("report", 1)[1]
    assert timing["if"].startswith("${{ always()")
    assert timing["continue-on-error"] is True, "reporting never decides a run"
    assert steps.index("Run selected stand suite") < steps.index(TIMING_STEP)
    assert steps.index(TIMING_STEP) < steps.index("Admit cleanup handoff")


# --- every compose call that can start a container runs the pulled release ----------------
#
# Bring-up leaves no `codegen-orchestrator/*:local` image on the stand. A compose `up`,
# `create`, `run` or `start` without the release override would find none and build the
# service from the checkout; one without `--no-build --pull never` could build or pull
# anyway. `start`, `restart` and `run` take no `--no-build`, so on the stand they cannot
# be reached at all. The runner's own calls are pinned in scripts/tests/test_stand_run.py.

POLICED_COMPOSE_VERBS = ("up", "create", "run", "start", "restart")
RELEASE_POLICY = "--no-build --pull never"
_COMPOSE_CALL = re.compile(r"docker compose\b|\$\{COMPOSE\}|\$COMPOSE\b")
_COMPOSE_ASSIGNMENT = re.compile(r'^\s*COMPOSE="([^"]*)"')
_OVERRIDE_REFERENCES = ("${override}", "${STAND_SERVICE_RELEASE_COMPOSE}")


def _compose_invocations(script: str) -> list[tuple[list[str], str, str]]:
    """Every compose call a step script makes: (files, verb, the rest of the line).

    `${COMPOSE}` is expanded to what the script last assigned it, the way the shell
    reads it, so an override appended to the variable counts where it was appended.
    """
    compose: str | None = None
    calls: list[tuple[list[str], str, str]] = []
    for line in _joined(script).splitlines():
        if line.strip().startswith("#"):
            continue
        assignment = _COMPOSE_ASSIGNMENT.match(line)
        if assignment:
            compose = assignment.group(1).replace("${COMPOSE}", compose or "")
            continue
        for occurrence in _COMPOSE_CALL.finditer(line):
            text = line[occurrence.start() :]
            if occurrence.group() != "docker compose":
                assert compose is not None, f"${{COMPOSE}} used before it is assigned: {line}"
                text = compose + text[len(occurrence.group()) :]
            tokens = text.split()[2:]
            files: list[str] = []
            while len(tokens) > 1 and tokens[0] in ("-f", "--file", "-p", "--project-name"):
                if tokens[0] in ("-f", "--file"):
                    files.append(tokens[1])
                tokens = tokens[2:]
            calls.append((files, tokens[0] if tokens else "", " ".join(tokens[1:])))
    return calls


def _release_violations(script: str) -> list[str]:
    violations = []
    for files, verb, rest in _compose_invocations(script):
        if verb == "build" or re.search(r"(?<![\w-])--build\b", rest):
            violations.append(f"{verb} {rest}: builds")
        if verb not in POLICED_COMPOSE_VERBS:
            continue
        if not any(reference in files for reference in _OVERRIDE_REFERENCES):
            violations.append(f"{verb} {rest}: without the release override")
        if RELEASE_POLICY not in rest:
            violations.append(f"{verb} {rest}: without {RELEASE_POLICY}")
    return violations


def test_the_compose_scanner_catches_a_call_that_would_build_on_the_stand():
    """The contract below is only as good as this reader, so it is shown a bad script."""
    base = 'COMPOSE="docker compose -f docker-compose.yml -f docker-compose.prod.yml"\n'
    released = base + 'COMPOSE="${COMPOSE} -f ${override}"\n'

    assert _release_violations(released + "${COMPOSE} up -d --no-build --pull never api\n") == []
    assert _release_violations(base + "${COMPOSE} up -d --no-build --pull never api\n")
    assert _release_violations(released + "${COMPOSE} up -d --force-recreate api\n")
    assert _release_violations(released + "${COMPOSE} create --no-build --pull never\n") == []
    assert _release_violations(released + "${COMPOSE} create api\n")
    assert _release_violations(released + "${COMPOSE} start api\n")
    assert _release_violations(released + "${COMPOSE} run --rm api true\n")
    assert _release_violations("ssh host 'docker compose -f a.yml -f ${override} restart api'\n")
    assert _release_violations(released + "${COMPOSE} build api\n")
    assert _release_violations(base + "${COMPOSE} exec -T api true\n") == []
    assert _release_violations("# docker compose up -d, in a comment\n") == []


def test_every_compose_call_that_starts_a_container_runs_the_pulled_release():
    starting = []
    for job in _workflow()["jobs"].values():
        for step in job["steps"]:
            script = step.get("run", "")
            assert _release_violations(script) == [], step.get("name")
            starting += [
                verb
                for _files, verb, _rest in _compose_invocations(script)
                if verb in POLICED_COMPOSE_VERBS
            ]
    # Not vacuous: bring-up's two `up` calls are the ones the reader has to have seen.
    assert starting == ["up", "up"]


def test_the_suite_step_hands_the_runner_the_override_bring_up_generated(tmp_path):
    """The runner recreates services on a QA switch; it does so from this file."""
    background = tmp_path / "bg"
    commands = _run_step_against_fake_ssh(
        tmp_path,
        "Run selected stand suite",
        {
            "STAND_BACKGROUND_DIR": str(background),
            "SUITE": "mega-live",
            "WORKER": "claude",
            "QA": "codex",
            "TEMPLATE_SOURCE": "",
            "TEMPLATE_REF": "",
            "STAND_PRODUCT_BOT_TOKEN": "",
        },
    )
    assert len(commands) == 1
    host = _stand_host(tmp_path)
    _finished_job(background, "uv", 0, "synced\n")
    stand_bin = tmp_path / "stand-bin"
    _write_executable(
        stand_bin / "uv",
        '#!/usr/bin/env bash\necho "uv $* override=${STAND_SERVICE_RELEASE_COMPOSE:-unset} '
        'frozen=${UV_FROZEN:-unset}"\n',
    )

    result = subprocess.run(
        ["bash", "-c", _on_host(commands[0], host)],
        input="\n",
        capture_output=True,
        text=True,
        env={"PATH": f"{stand_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    override = _job_env()["STAND_SERVICE_RELEASE_COMPOSE"]
    assert "python -m scripts.stand_run --suite mega-live" in result.stdout
    assert f"override={override} frozen=1" in result.stdout
    # The same name bring-up writes the override to, in the checkout the runner resolves
    # it against, and the same variable the runner reads.
    bring_up = _steps()[BRING_UP_STEP]["run"]
    assert "override=${STAND_SERVICE_RELEASE_COMPOSE}" in bring_up
    assert '--output "${override}"' in bring_up
    from scripts import stand_run

    assert stand_run.SERVICE_RELEASE_OVERRIDE_ENV == "STAND_SERVICE_RELEASE_COMPOSE"


# --- The QA Telegram session: validated, rendered for qa-worker, proven, redacted ---

#: Credentials pre-create validation requires that are deliberately not stand
#: configuration, each with the reason. A name validated before any machine exists
#: and then dropped from the render is either listed here or a defect.
NOT_NEEDED_ON_STAND = {
    "SSH_PRIVATE_KEY": (
        "the runner's own key for reaching the pair it creates: written to protected key "
        "files for ssh, bootstrap and target registration, never a service setting"
    ),
}
QA_WORKER_ENV = "/opt/codegen_orchestrator/.qa-worker.env"
TELETHON_NAMES = ("TELETHON_API_ID", "TELETHON_API_HASH", "TELETHON_SESSION")


def _render_script(step: dict) -> str:
    """The Python program of the render step, as the runner executes it."""
    return step["run"].split("python3 -c '\n", maxsplit=1)[1].rsplit("\n'", maxsplit=1)[0]


def _rendered_files(step: dict) -> dict[str, tuple[str, ...]]:
    """The names each render tuple writes: `names` to .stand.env, `telethon` to qa-worker."""
    tuples = {}
    for node in ast.walk(ast.parse(_render_script(step))):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Tuple)
        ):
            tuples[node.targets[0].id] = tuple(ast.literal_eval(node.value))
    return {".stand.env": tuples["names"], ".stand-qa-worker.env": tuples["telethon"]}


def _validated_but_dropped(steps: dict[str, dict]) -> set[str]:
    """Validated credentials whose secret no rendered stand setting carries."""
    validated = steps["Validate pre-create credentials"]["env"]
    render = steps["Render protected dynamic configuration"]
    rendered = {name for names in _rendered_files(render).values() for name in names}
    carried = {render["env"][name] for name in rendered if name in render["env"]}
    return {
        name
        for name, source in validated.items()
        if source not in carried and name not in NOT_NEEDED_ON_STAND
    }


def test_every_validated_credential_is_rendered_for_the_stand_or_named_as_not_needed():
    steps = _steps()

    assert _validated_but_dropped(steps) == set()
    # The list is the validation's own: each name it is handed is one it requires,
    # and nothing it requires is outside the list.
    now = datetime(2026, 9, 25, tzinfo=UTC)
    valid = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-fake-opaque-claude-token",
        "CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT": (
            now + CLAUDE_MINIMUM_TTL + timedelta(seconds=1)
        ).isoformat(),
        "TELETHON_API_ID": "12345",
        "TELETHON_API_HASH": "hash",
        "TELETHON_SESSION": "session",
        "SSH_PRIVATE_KEY": (
            "-----BEGIN OPENSSH PRIVATE KEY-----\ndGVzdA==\n-----END OPENSSH PRIVATE KEY-----"
        ),
    }
    validated = steps["Validate pre-create credentials"]["env"]
    assert set(valid) == set(validated)
    assert validate_precreate_credentials(valid, now=now) == []
    for name in validated:
        assert validate_precreate_credentials({**valid, name: ""}, now=now), name
    assert set(NOT_NEEDED_ON_STAND) <= set(validated)


def test_a_validated_credential_removed_from_the_render_fails_the_list_test():
    workflow = _workflow()
    steps = {step["name"]: step for step in workflow["jobs"]["e2e"]["steps"]}
    render = steps["Render protected dynamic configuration"]
    render["run"] = render["run"].replace(
        '"TELETHON_API_HASH", "TELETHON_SESSION")', '"TELETHON_API_HASH")'
    )

    assert _validated_but_dropped(steps) == {"TELETHON_SESSION"}


def _run_render(tmp_path: Path, *, qa_telethon: str, **overrides: str):
    step = _steps()["Render protected dynamic configuration"]
    environment = {name: f"value-of-{name}" for name in step["env"]}
    environment.update(GH_APP_PRIVATE_KEY="key", QA_TELETHON=qa_telethon, **overrides)
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _render_script(step)],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"], **environment},
        capture_output=True,
        text=True,
        check=False,
    )
    return result


def test_the_qa_session_is_required_for_a_suite_that_opens_it():
    step = _steps()["Render protected dynamic configuration"]
    suite = _steps()["Resolve the suite"]

    assert step["env"]["QA_TELETHON"] == "${{ steps.suite.outputs.qa_telethon }}"
    assert "scripts.stand_telethon_preflight needs-session" in suite["run"]
    assert "qa_telethon=" in suite["run"]
    assert needs_session("mega-live") is True
    assert needs_session("mega-noop") is False
    for name in TELETHON_NAMES:
        assert step["env"][name] == f"${{{{ secrets.{name} }}}}"


def test_mega_live_refuses_to_render_without_the_qa_session(tmp_path):
    result = _run_render(tmp_path, qa_telethon="true", TELETHON_SESSION="")

    assert result.returncode != 0
    assert "missing required qa-worker configuration: TELETHON_SESSION" in result.stderr
    assert not (tmp_path / ".stand.env").exists()
    assert not (tmp_path / ".stand-qa-worker.env").exists()


def test_mega_live_renders_the_qa_session_for_qa_worker_and_not_into_the_stand_env(tmp_path):
    result = _run_render(tmp_path, qa_telethon="true")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".stand-qa-worker.env").read_text() == "".join(
        f"{name}=value-of-{name}\n" for name in TELETHON_NAMES
    )
    assert "TELETHON" not in (tmp_path / ".stand.env").read_text()


def test_mega_noop_renders_without_the_qa_session_as_it_always_has(tmp_path):
    result = _run_render(tmp_path, qa_telethon="false", **dict.fromkeys(TELETHON_NAMES, ""))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".stand-qa-worker.env").read_text() == "".join(
        f"{name}=\n" for name in TELETHON_NAMES
    )
    assert "TELETHON" not in (tmp_path / ".stand.env").read_text()


def test_the_qa_worker_env_file_reaches_the_stand_beside_its_env_and_is_never_rsynced():
    bring_up = _steps()["Bring up dynamic orchestrator and wait for API"]["run"]
    bootstrap = _steps()["Bootstrap dynamic orchestrator"]["run"]

    assert re.search(
        r'\.stand-qa-worker\.env \\\n\s+root@"\$\{PROD_HOST\}":' + re.escape(QA_WORKER_ENV) + "\n",
        bring_up,
    )
    assert bring_up.index(".stand-qa-worker.env") < bring_up.index("up -d --remove-orphans")
    assert "--exclude .stand-qa-worker.env" in bootstrap


def _render_stand_stack(project_dir: Path, qa_worker_env: str | None) -> dict:
    """The stand stack as compose resolves it, from a stand .env without the session."""
    root = WORKFLOW.parents[2]
    env_file = project_dir / ".env"
    example = (root / ".env.example").read_text().splitlines()
    env_file.write_text(
        "\n".join(line for line in example if not line.startswith("TELETHON_"))
        + "\nLOKI_URL=http://loki:3100\nHOST_CODEX_HOME=/opt/secrets/codex-stand\n"
    )
    if qa_worker_env is not None:
        (project_dir / ".qa-worker.env").write_text(qa_worker_env)
    command = ["docker", "compose", "--project-directory", str(project_dir)]
    command += ["--env-file", str(env_file)]
    for name in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.stand.yml"):
        command += ["-f", str(root / name)]
    result = subprocess.run(  # noqa: S603
        [*command, "config", "--format", "json"], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def _telethon_by_service(config: dict) -> dict[str, dict[str, str]]:
    found = {}
    for name, service in config["services"].items():
        environment = service.get("environment") or {}
        telethon = {key: value for key, value in environment.items() if key.startswith("TELETHON_")}
        if telethon:
            found[name] = telethon
    return found


def test_the_qa_session_reaches_qa_worker_and_no_other_service_on_the_stand(tmp_path):
    session = "1" + "A" * 40
    config = _render_stand_stack(
        tmp_path,
        f"TELETHON_API_ID=12345\nTELETHON_API_HASH=feed\nTELETHON_SESSION={session}\n",
    )

    assert _telethon_by_service(config) == {
        "qa-worker": {
            "TELETHON_API_ID": "12345",
            "TELETHON_API_HASH": "feed",
            "TELETHON_SESSION": session,
        }
    }
    # qa-worker still reads the stand .env like every other service.
    assert config["services"]["qa-worker"]["environment"]["POSTGRES_DB"]


def test_a_stand_without_the_qa_worker_env_file_still_renders(tmp_path):
    assert _telethon_by_service(_render_stand_stack(tmp_path, None)) == {}


def test_the_qa_session_is_proven_before_any_paid_step_and_only_when_a_suite_opens_it():
    steps = list(_steps())
    proof = _steps()["Prove the QA Telegram session"]

    assert proof["if"] == "${{ steps.suite.outputs.qa_telethon == 'true' }}"
    assert "python -m scripts.stand_telethon_preflight prove" in proof["run"]
    assert "--with 'telethon==1.45.0'" in proof["run"]
    for name in (*TELETHON_NAMES, "STAND_PRODUCT_BOT_TOKEN"):
        assert proof["env"][name] == f"${{{{ secrets.{name} }}}}"
        assert f"${{{name}}}" not in proof["run"]
    assert proof["timeout-minutes"] <= 10
    position = steps.index("Prove the QA Telegram session")
    assert steps.index("Install uv") < position
    assert steps.index("Resolve the suite") < position
    assert steps.index("Validate pre-create credentials") < position
    for paid in (
        "Authenticate Codex against exact worker image",
        "Preflight ephemeral machines",
        "Create ephemeral machines",
        "Give the stand a resolvable name",
        "Run selected stand suite",
    ):
        assert position < steps.index(paid), paid


def _remote_redaction_scripts() -> list[str]:
    return [
        _steps()["Register and provision dynamic target"]["run"],
        _steps()["Record machine manifest"]["run"],
    ]


def _session_needles(script: str) -> tuple[str, str]:
    """The shell that reads the session for the redactor, and the redactor itself."""
    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "qa_env=./.qa-worker.env")
    end = next(i for i, line in enumerate(lines) if line.strip().startswith("protected_names="))
    shell = textwrap.dedent("\n".join(lines[start : end + 1]))
    code = re.search(r'python -c "((?:[^"\\]|\\.)*)"', script).group(1).replace('\\"', '"')
    return shell, code


@pytest.mark.parametrize("which", [0, 1], ids=["provisioning-tails", "suite-failure-tails"])
def test_the_service_tail_redaction_removes_a_session_looking_value(tmp_path, which):
    script = _remote_redaction_scripts()[which]
    session = "1" + base64.urlsafe_b64encode(os.urandom(263)).decode()
    api_hash = "0123456789abcdef0123456789abcdef"
    (tmp_path / ".qa-worker.env").write_text(
        f"TELETHON_API_ID=12345\nTELETHON_API_HASH={api_hash}\nTELETHON_SESSION={session}\n"
    )
    shell, code = _session_needles(script)
    assert "-e TELETHON_API_HASH -e TELETHON_SESSION api" in script

    # What the stand host's shell hands the redactor: the names, and the values by name.
    read = subprocess.run(  # noqa: S603
        [
            "bash",
            "-c",
            f'set -euo pipefail\n{shell}\nprintf "%s\\0" "${{protected_names}}" '
            '"${TELETHON_API_HASH}" "${TELETHON_SESSION}"',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    names, handed_hash, handed_session = read.stdout.split("\0")[:3]
    assert {"TELETHON_API_HASH", "TELETHON_SESSION"} <= set(names.split())
    assert (handed_hash, handed_session) == (api_hash, session)

    tail = (
        f"qa-worker | telethon session={session}\n"
        f"qa-worker | api_hash {api_hash} in a traceback\n"
        "qa-worker | qa_telethon_not_configured\n"
    )
    redacted = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        input=tail,
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(WORKFLOW.parents[2]),
            "STAND_DIAGNOSTIC_SECRET_NAMES": names,
            "TELETHON_API_HASH": handed_hash,
            "TELETHON_SESSION": handed_session,
        },
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert session not in redacted
    assert api_hash not in redacted
    assert redacted.count("[redacted]") >= 2
    assert "qa_telethon_not_configured" in redacted


def test_the_qa_session_is_a_protected_value_of_every_artifact_admission():
    assert {"TELETHON_API_HASH", "TELETHON_SESSION"} <= PROTECTED_STAND_SECRET_NAMES
    # TELETHON_API_ID is an application number, not a credential: never a needle.
    assert "TELETHON_API_ID" not in PROTECTED_STAND_SECRET_NAMES
