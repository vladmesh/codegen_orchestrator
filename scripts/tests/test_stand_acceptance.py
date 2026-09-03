import base64
from datetime import UTC, datetime, timedelta
import json

from scripts.stand_acceptance import (
    ADMISSION_MARKER,
    PROTECTED_STAND_SECRET_NAMES,
    _emit_admission_diagnostics,
    admit_artifact,
    admit_artifact_from_environment,
    build_acceptance_artifact,
    protected_values_from_environment,
    run_dir_needs_target_snapshot,
    scan_artifact,
    target_snapshot_required,
)
from scripts.stand_preflight import check_stand_token_credentials


def _protected_environment() -> dict[str, str]:
    return {
        name: f"protected-{index}"
        for index, name in enumerate(sorted(PROTECTED_STAND_SECRET_NAMES))
    }


def _jwt_with_expiry(expiry: datetime) -> str:
    payload = json.dumps({"exp": int(expiry.timestamp())}).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


def _stand_preflight_refusal() -> str:
    name, passed, detail = check_stand_token_credentials()
    assert passed is False
    return f"{'ok  ' if passed else 'FAIL'} {name}{f': {detail}' if detail else ''}\n"


def _handoff_candidate(root, log_text: str) -> None:
    run = root / "run"
    run.mkdir(parents=True)
    (root / "machines.json").write_text('{"machines": []}\n', encoding="utf-8")
    (run / "junit.xml").write_text("<testsuites/>\n", encoding="utf-8")
    (run / "report.tsv").write_text("status\nfailed\n", encoding="utf-8")
    (run / "run.log").write_text(log_text, encoding="utf-8")


def _final_candidate(root, log_text: str) -> None:
    root.mkdir()
    (root / "machines.json").write_text('{"machines": []}\n', encoding="utf-8")
    (root / "final-report.json").write_text('{"status": "incomplete"}\n', encoding="utf-8")
    (root / "junit.xml").write_text("<testsuites/>\n", encoding="utf-8")
    (root / "report.tsv").write_text("status\nfailed\n", encoding="utf-8")
    (root / "run.log").write_text(log_text, encoding="utf-8")


def _write_inputs(tmp_path, *, cleanup: dict | None = None):
    manifest = tmp_path / "machines.json"
    manifest.write_text(
        json.dumps(
            {
                "run_tag": "gha-17",
                "observed_at": "2026-08-28T00:00:00Z",
                "resource_ceiling": 2,
                "lifetime_seconds": 21600,
                "machines": [
                    {
                        "id": "one",
                        "role": "orchestrator",
                        "ip": "203.0.113.1",
                        "observed_at": "2026-08-28T00:00:00Z",
                        "run_tag": "gha-17",
                        "created_at": "2026-08-28T00:00:00Z",
                        "rate_milliusd_per_hour": 42,
                        "rate_unit": "USD*1000 per hour",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.log").write_text("suite passed\n", encoding="utf-8")
    (run_dir / "report.tsv").write_text(
        "qa_agent\tworker_agent\tstatus\tduration_seconds\ncodex\tclaude\tpassed\t3\n",
        encoding="utf-8",
    )
    (run_dir / "junit.xml").write_text("<testsuites/>\n", encoding="utf-8")
    cleanup_path = tmp_path / "cleanup.json"
    cleanup_path.write_text(
        json.dumps(
            cleanup
            or {
                "run_tag": "gha-17",
                "observed_at": "2026-08-28T01:00:00Z",
                "selected_ids": ["one"],
                "deleted_ids": ["one"],
                "remaining_ids": [],
                "servers_used": 0,
                "status": "verified",
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    return manifest, run_dir, cleanup_path


def test_build_acceptance_artifact_records_observed_lifetime_and_rate_estimate(tmp_path):
    manifest, run_dir, cleanup = _write_inputs(tmp_path)
    output = tmp_path / "acceptance"

    complete = build_acceptance_artifact(manifest, run_dir, cleanup, output)

    report = json.loads((output / "final-report.json").read_text(encoding="utf-8"))
    assert complete is True
    assert report["status"] == "complete"
    assert report["machines"][0]["lifetime_seconds"] == 3600
    assert report["machines"][0]["cost"]["status"] == "estimated"
    assert report["machines"][0]["cost"]["usd"] == "0.042"
    assert report["machines"][0]["cost"]["billed_hours"] == 1
    assert report["run_cost"]["status"] == "estimated"
    assert report["run_cost"]["usd"] == "0.042"
    assert sorted(path.name for path in output.iterdir()) == [
        "final-report.json",
        "junit.xml",
        "machines.json",
        "report.tsv",
        "run.log",
    ]


NOOP_EVIDENCE_NAME = "run-evidence-worker-noop-qa-health-20260831T225500.json"
PAID_EVIDENCE_NAME = "run-evidence-worker-claude-qa-codex-20260902T081500.json"


def _missed(reason: str) -> dict:
    return {"status": "missed", "value": None, "reason": reason}


def _captured(value: object) -> dict:
    return {"status": "captured", "value": value, "reason": None}


def _run_evidence(*, paid: bool, failed: bool, **overrides) -> dict:
    """One run-evidence artifact of the shape the live harness writes today."""
    evidence = {
        "schema_version": 7,
        "kind": "worker_failure_attribution",
        "failure": {
            "failed": failed,
            "stage": "stopped_at_engineering" if failed else "completed",
            "failure_kind": "worker_did_not_finish" if failed else "none",
            "control_plane_reason": {
                "status": "captured",
                "value": {"source": "engineering_run", "runs": [{"run_status": "failed"}]},
                "reason": None,
            },
        },
        "verdict": {
            "paid": paid,
            "status": "red" if failed else "green",
            "reasons": [{"code": "run_failed", "detail": "stopped at engineering"}]
            if failed
            else [],
        },
        "engineering": {
            "collection": {"status": "captured", "value": {"runs_read": 1}, "reason": None},
            "runs": [{"run_id": "run-1"}],
        },
        "tasks": {
            "task-1": {
                "status": "waiting_human_review",
                "failure_metadata": {"reason": "worker exited 7"},
            }
        },
        "workers": [{"exit_code": {"value": 7}, "log_tail": {"text": "safe tail"}}],
        "qa": {
            "run_record": _missed(
                "no QA Run record was read: the worker died before QA: the engineering task "
                "ended failed, so nothing was ever handed to QA"
            ),
        },
        "deployment": {
            "deployed_url": None,
            "run_record": _missed(
                "no deploy Run record was read: this combination never reached a deploy Run"
            ),
            "reachability": {
                "deploy_smoke": _missed("the deploy smoke evidence is unread with its Run record"),
                "harness_probe": _missed("the harness ran no health probe of its own"),
                "qa_probe": _missed("what QA got from the deployed URL is unread"),
                "target_host_snapshot": {
                    "required": False,
                    "file": "target-app.log",
                    "reason": "the deploy did not report success, so an unanswered deployed "
                    "URL is already accounted for by the deploy stage",
                    "collection": _missed("this run asked for no snapshot of the target host"),
                },
            },
        },
    }
    evidence.update(overrides)
    return evidence


def _qa_stage_evidence(*, paid: bool = True, required: bool = True) -> dict:
    """A run of run 33711527100's shape: deploy success, QA blocked unreachable."""
    evidence = _run_evidence(paid=paid, failed=True)
    evidence["failure"]["stage"] = "stopped_at_qa"
    evidence["failure"]["failure_kind"] = "qa_not_passed"
    evidence["qa"]["run_record"] = _captured(
        {
            "id": "qa-deploy-poll-93440111",
            "status": "completed",
            "qa_outcome": "blocked",
            "blocker": {
                "category": "deployed_url_unreachable",
                "attempted": "GET deployed URL before starting QA agent",
                "sent": "GET http://198.51.100.7:8000",
                "received": "transport error: All connection attempts failed",
            },
        }
    )
    deployment = evidence["deployment"]
    deployment["run_record"] = _captured(
        {"id": "deploy-1", "deploy_outcome": "success", "smoke_result": {"passed": True}}
    )
    deployment["reachability"]["deploy_smoke"] = _captured({"passed": True})
    deployment["reachability"]["qa_probe"] = _captured({"reached_the_url": False})
    deployment["reachability"]["target_host_snapshot"] = {
        "required": required,
        "file": "target-app.log",
        "reason": "the deploy reported success and QA's own probe got no response",
        "collection": (
            _captured({"file": "target-app.log", "characters": 812})
            if required
            else _missed("this run asked for no snapshot of the target host")
        ),
    }
    return evidence


def test_build_and_admission_preserve_redacted_worker_run_evidence(tmp_path):
    manifest, run_dir, cleanup = _write_inputs(tmp_path)
    evidence = _run_evidence(paid=False, failed=False)
    (run_dir / NOOP_EVIDENCE_NAME).write_text(json.dumps(evidence), encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True
    assert json.loads((output / NOOP_EVIDENCE_NAME).read_text(encoding="utf-8")) == evidence
    assert scan_artifact(output, canaries=("not-present",)) == []


def _paid_failure_inputs(tmp_path, evidence: dict, *, service_log: bool = True):
    manifest, run_dir, cleanup = _write_inputs(tmp_path)
    (run_dir / PAID_EVIDENCE_NAME).write_text(json.dumps(evidence), encoding="utf-8")
    if service_log:
        (run_dir / "suite-services.log").write_text(
            "scheduler | admitted paid run\n", encoding="utf-8"
        )
    return manifest, run_dir, cleanup


def _incompleteness(output) -> list[str]:
    return json.loads((output / "final-report.json").read_text(encoding="utf-8"))["incompleteness"]


def test_a_failed_paid_run_arrives_with_its_stage_reason_and_service_tails(tmp_path):
    manifest, run_dir, cleanup = _paid_failure_inputs(
        tmp_path, _run_evidence(paid=True, failed=True)
    )
    (run_dir / "debug-full-llm-engineering-20260902-081500.md").write_text(
        "# Debug\n- task_status: `failed`\n", encoding="utf-8"
    )
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True
    assert (output / "suite-services.log").is_file()
    assert (output / "debug-full-llm-engineering-20260902-081500.md").is_file()
    assert scan_artifact(output, canaries=("not-present",)) == []


def test_a_debug_dump_named_with_an_underscore_reaches_the_handoff(tmp_path):
    """A dump name the harness can actually produce is collected, not dropped."""
    manifest, run_dir, cleanup = _paid_failure_inputs(
        tmp_path, _run_evidence(paid=True, failed=True)
    )
    dump = "debug-test_full_pipeline_LLM-20260902-081500.md"
    (run_dir / dump).write_text("# Debug\n- task_status: `failed`\n", encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True
    assert (output / dump).is_file()
    assert scan_artifact(output, canaries=("not-present",)) == []


def test_a_dump_shaped_file_the_handoff_cannot_carry_is_named_not_dropped(tmp_path):
    manifest, run_dir, cleanup = _paid_failure_inputs(
        tmp_path, _run_evidence(paid=True, failed=True)
    )
    dump = "debug-no-timestamp.md"
    (run_dir / dump).write_text("# Debug\n", encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert f"debug_dump_name_unadmissible:{dump}" in _incompleteness(output)
    assert not (output / dump).exists()


def test_a_debug_dump_the_run_declared_but_did_not_arrive_is_named(tmp_path):
    evidence = _run_evidence(paid=True, failed=True)
    evidence["debug_dumps"] = ["debug-full-llm-engineering-20260902-081500.md"]
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert (
        "debug_dump_not_collected:debug-full-llm-engineering-20260902-081500.md"
        in _incompleteness(output)
    )


def test_a_failed_paid_run_without_a_control_plane_reason_is_refused(tmp_path):
    evidence = _run_evidence(paid=True, failed=True)
    evidence["failure"]["control_plane_reason"] = {
        "status": "captured",
        "value": None,
        "reason": None,
    }
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert f"paid_failure_reason_missing:{PAID_EVIDENCE_NAME}" in _incompleteness(output)


def test_a_failed_paid_run_whose_reason_could_not_be_read_is_admitted_with_the_why(tmp_path):
    evidence = _run_evidence(paid=True, failed=True)
    evidence["failure"]["control_plane_reason"] = {
        "status": "missed",
        "value": None,
        "reason": "the control plane holds no engineering Run for the failed task",
    }
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True


def test_a_failed_paid_run_without_the_new_evidence_sections_is_refused(tmp_path):
    evidence = _run_evidence(paid=True, failed=True)
    del evidence["failure"]
    del evidence["verdict"]
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert f"run_evidence_failure_section_missing:{PAID_EVIDENCE_NAME}" in _incompleteness(output)


def test_a_failed_paid_run_without_service_log_tails_is_refused(tmp_path):
    manifest, run_dir, cleanup = _paid_failure_inputs(
        tmp_path, _run_evidence(paid=True, failed=True), service_log=False
    )
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert "paid_failure_service_diagnostics_missing" in _incompleteness(output)


def test_a_passing_paid_run_needs_no_service_log_tails(tmp_path):
    manifest, run_dir, cleanup = _paid_failure_inputs(
        tmp_path, _run_evidence(paid=True, failed=False), service_log=False
    )
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True


def test_a_qa_stage_failure_arrives_with_the_target_host_snapshot_it_asked_for(tmp_path):
    """Run 33711527100's shape: the artifact asks, and the snapshot is admitted."""
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, _qa_stage_evidence())
    (run_dir / "target-app.log").write_text(
        "== containers ==\nbackend image=ghcr.io/org/app state=exited status=Exited (1)\n",
        encoding="utf-8",
    )
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True
    assert "exited" in (output / "target-app.log").read_text(encoding="utf-8")
    assert scan_artifact(output, canaries=("not-present",)) == []


def test_a_qa_stage_failure_without_the_target_snapshot_it_asked_for_is_refused(tmp_path):
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, _qa_stage_evidence())
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert "paid_failure_target_snapshot_missing" in _incompleteness(output)


def test_a_failure_that_asked_for_no_target_snapshot_needs_none(tmp_path):
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, _qa_stage_evidence(required=False))
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True


def test_a_failed_paid_run_without_a_qa_or_deploy_run_record_is_refused(tmp_path):
    evidence = _run_evidence(paid=True, failed=True)
    del evidence["qa"]["run_record"]
    evidence["deployment"]["run_record"] = {"status": "captured", "value": None, "reason": None}
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    incompleteness = _incompleteness(output)
    assert f"paid_failure_qa_run_record_missing:{PAID_EVIDENCE_NAME}" in incompleteness
    assert f"paid_failure_deploy_run_record_missing:{PAID_EVIDENCE_NAME}" in incompleteness


def test_a_failed_paid_run_that_cannot_say_what_the_url_answered_is_refused(tmp_path):
    evidence = _qa_stage_evidence()
    del evidence["deployment"]["reachability"]["harness_probe"]
    evidence["deployment"]["reachability"]["target_host_snapshot"] = {"required": True}
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    (run_dir / "target-app.log").write_text("== containers ==\n", encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    incompleteness = _incompleteness(output)
    assert (
        f"paid_failure_reachability_read_missing:{PAID_EVIDENCE_NAME}:harness_probe"
        in incompleteness
    )
    assert (
        f"paid_failure_target_snapshot_requirement_missing:{PAID_EVIDENCE_NAME}" in incompleteness
    )


def test_a_run_that_cannot_say_what_became_of_its_snapshot_is_refused(tmp_path):
    evidence = _qa_stage_evidence()
    del evidence["deployment"]["reachability"]["target_host_snapshot"]["collection"]
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    (run_dir / "target-app.log").write_text("== containers ==\n", encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert (
        f"paid_failure_target_snapshot_collection_missing:{PAID_EVIDENCE_NAME}"
        in _incompleteness(output)
    )


def test_a_snapshot_the_suite_could_not_take_is_admitted_with_the_stated_why(tmp_path):
    """The suite names why it could not collect; the workflow names the absent file."""
    evidence = _qa_stage_evidence()
    evidence["deployment"]["reachability"]["target_host_snapshot"]["collection"] = _missed(
        "the target host snapshot command exited 255: ssh: connect refused"
    )
    manifest, run_dir, cleanup = _paid_failure_inputs(tmp_path, evidence)
    (run_dir / "target-app.log").write_text(
        "the target host snapshot is unavailable: the suite asked for a target host snapshot "
        "and none reached the runner directory (suite step outcome: failure)\n",
        encoding="utf-8",
    )
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True
    assert "none reached the runner directory" in (
        (output / "target-app.log").read_text(encoding="utf-8")
    )


def test_the_workflow_and_the_admission_read_one_target_snapshot_predicate(tmp_path):
    """The step that collects and the boundary that refuses cannot disagree."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert run_dir_needs_target_snapshot(run_dir) is False

    (run_dir / PAID_EVIDENCE_NAME).write_text(
        json.dumps(_qa_stage_evidence(required=False)), encoding="utf-8"
    )
    assert run_dir_needs_target_snapshot(run_dir) is False

    (run_dir / PAID_EVIDENCE_NAME).write_text(json.dumps(_qa_stage_evidence()), encoding="utf-8")
    assert run_dir_needs_target_snapshot(run_dir) is True
    assert target_snapshot_required(_qa_stage_evidence()) is True


def test_unreadable_evidence_asks_for_no_target_snapshot_and_is_refused_on_its_own(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / PAID_EVIDENCE_NAME).write_text("{not json", encoding="utf-8")

    assert run_dir_needs_target_snapshot(run_dir) is False


def test_an_unreadable_run_evidence_file_is_named_not_skipped(tmp_path):
    manifest, run_dir, cleanup = _write_inputs(tmp_path)
    (run_dir / PAID_EVIDENCE_NAME).write_text("{not json", encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is False
    assert f"run_evidence_unreadable:{PAID_EVIDENCE_NAME}" in _incompleteness(output)


def test_a_free_noop_failure_is_not_held_to_the_paid_failure_evidence(tmp_path):
    """The free suite keeps its admission semantics exactly as they were."""
    manifest, run_dir, cleanup = _write_inputs(tmp_path)
    evidence = _run_evidence(paid=False, failed=True)
    evidence["failure"]["control_plane_reason"] = {
        "status": "captured",
        "value": None,
        "reason": None,
    }
    (run_dir / NOOP_EVIDENCE_NAME).write_text(json.dumps(evidence), encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True


def test_build_and_admission_preserve_provisioning_failure_diagnostics(tmp_path):
    manifest, run_dir, cleanup = _write_inputs(tmp_path)
    state = '{"status":"error","provisioning_phase":"software_installation"}\n'
    services = "infra-service | ansible_playbook_timeout timeout=1200\n"
    (run_dir / "provisioning-state.jsonl").write_text(state, encoding="utf-8")
    (run_dir / "provisioning-services.log").write_text(services, encoding="utf-8")
    output = tmp_path / "acceptance"

    assert build_acceptance_artifact(manifest, run_dir, cleanup, output) is True
    assert (output / "provisioning-state.jsonl").read_text(encoding="utf-8") == state
    assert (output / "provisioning-services.log").read_text(encoding="utf-8") == services
    assert scan_artifact(output, canaries=("not-present",)) == []


def test_build_marks_missing_cleanup_observation_incomplete_without_inventing_cost(tmp_path):
    cleanup = {
        "run_tag": "gha-17",
        "selected_ids": ["one"],
        "deleted_ids": ["one"],
        "remaining_ids": [],
        "servers_used": None,
        "status": "incomplete",
        "errors": ["account_observation_unusable"],
    }
    manifest, run_dir, cleanup_path = _write_inputs(tmp_path, cleanup=cleanup)
    output = tmp_path / "acceptance"

    complete = build_acceptance_artifact(manifest, run_dir, cleanup_path, output)

    report = json.loads((output / "final-report.json").read_text(encoding="utf-8"))
    assert complete is False
    assert report["status"] == "incomplete"
    assert report["machines"][0]["lifetime_seconds"] is None
    assert report["machines"][0]["cost"]["usd"] is None
    assert report["run_cost"]["usd"] is None


def test_build_uses_only_an_exactly_correlated_usage_cost_as_actual(tmp_path):
    cleanup = {
        "run_tag": "gha-17",
        "observed_at": "2026-08-28T01:00:00Z",
        "selected_ids": ["one"],
        "deleted_ids": ["one"],
        "remaining_ids": [],
        "servers_used": 0,
        "status": "verified",
        "errors": [],
        "usage": {
            "status": "observed",
            "observations": [
                {
                    "machine_id": "one",
                    "description": "codegen-stand-gha-17-orchestrator",
                    "cost_milliusd": 84,
                    "hours": 2,
                    "start": "2026-08-28T00:00:00Z",
                    "end": "2026-08-28T02:00:00Z",
                }
            ],
        },
    }
    manifest, run_dir, cleanup_path = _write_inputs(tmp_path, cleanup=cleanup)
    output = tmp_path / "acceptance"

    complete = build_acceptance_artifact(manifest, run_dir, cleanup_path, output)

    report = json.loads((output / "final-report.json").read_text(encoding="utf-8"))
    assert complete is True
    assert report["machines"][0]["cost"] == {
        "billed_hours": 2,
        "source": "provider_usage",
        "status": "actual",
        "unit": "USD*1000",
        "usd": "0.084",
    }
    assert report["run_cost"] == {
        "source": "provider_usage",
        "status": "actual",
        "unit": "USD*1000",
        "usd": "0.084",
    }


def test_build_refuses_a_malformed_or_uncorrelated_usage_observation(tmp_path):
    cleanup = {
        "run_tag": "gha-17",
        "observed_at": "2026-08-28T01:00:00Z",
        "selected_ids": ["one"],
        "deleted_ids": ["one"],
        "remaining_ids": [],
        "servers_used": 0,
        "status": "verified",
        "errors": [],
        "usage": {"status": "uncorrelated", "observations": []},
    }
    manifest, run_dir, cleanup_path = _write_inputs(tmp_path, cleanup=cleanup)
    output = tmp_path / "acceptance"

    complete = build_acceptance_artifact(manifest, run_dir, cleanup_path, output)

    report = json.loads((output / "final-report.json").read_text(encoding="utf-8"))
    assert complete is False
    assert report["run_cost"]["usd"] is None
    assert "run_owned_usage_uncorrelated" in report["incompleteness"]


def test_redaction_scan_rejects_a_supplied_fake_secret_without_echoing_it(tmp_path):
    artifact = tmp_path / "acceptance"
    artifact.mkdir()
    (artifact / "run.log").write_text("fake-secret-value\n", encoding="utf-8")

    errors = scan_artifact(artifact, canaries=("fake-secret-value",))

    assert errors == ["candidate contains a supplied redaction canary"]


def test_redaction_scan_covers_handoff_logs_and_bare_known_secret_values(tmp_path):
    handoff = tmp_path / "handoff"
    run = handoff / "run"
    run.mkdir(parents=True)
    (handoff / "machines.json").write_text("{}\n", encoding="utf-8")
    (run / "remote-invocation.log").write_text("bare-value\n", encoding="utf-8")

    errors = scan_artifact(handoff, canaries=("bare-value",))

    assert errors == ["candidate contains a supplied redaction canary"]


def test_admission_accepts_public_stand_configuration_and_real_shaped_manifest(tmp_path):
    handoff = tmp_path / "handoff"
    run = handoff / "run"
    run.mkdir(parents=True)
    (handoff / "machines.json").write_text(
        json.dumps(
            {
                "run_tag": "gha-33168872249-1",
                "machines": [
                    {
                        "id": "one",
                        "role": "orchestrator",
                        "ip": "203.0.113.10",
                        "created_at": "2026-08-28T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run / "run.log").write_text(
        "STAND_RUN_TAG=gha-33168872249-1\nLIVE_CONTOUR=stand\n"
        "PO_LLM_MODEL=gpt-public\nPOSTGRES_DB=codegen\n"
        "GITHUB_APP_PRIVATE_KEY_PATH=/app/keys/github_app.pem\n",
        encoding="utf-8",
    )
    status = tmp_path / "admission-status.json"

    admitted = admit_artifact(
        handoff,
        status_path=status,
        protected_values=("protected-value",),
    )

    assert admitted is True
    assert json.loads(status.read_text(encoding="utf-8")) == {
        "marker": ADMISSION_MARKER,
        "status": "admitted",
    }


def test_admission_rejects_bare_protected_value_and_credential_assignment_without_echoing_them(
    tmp_path,
):
    artifact = tmp_path / "acceptance"
    artifact.mkdir()
    (artifact / "run.log").write_text(
        "protected-value\nBITLAUNCH_API_KEY=another-value\n", encoding="utf-8"
    )
    status = tmp_path / "admission-status.json"

    admitted = admit_artifact(
        artifact,
        status_path=status,
        protected_values=("protected-value",),
    )

    assert admitted is False
    status_text = status.read_text(encoding="utf-8")
    assert json.loads(status_text) == {
        "issues": [
            {
                "path": "run.log",
                "reason": "candidate contains a supplied protected value",
            }
        ],
        "marker": ADMISSION_MARKER,
        "status": "rejected",
    }
    assert "protected-value" not in status_text
    assert "another-value" not in status_text


def test_admission_only_derives_needles_from_explicit_protected_name_allow_list():
    environment = _protected_environment()
    environment.update(
        {
            "STAND_RUN_TAG": "gha-33168872249-1",
            "LIVE_CONTOUR": "stand",
            "PO_LLM_MODEL": "gpt-public",
        }
    )
    values = protected_values_from_environment(environment)

    assert values == tuple(environment[name] for name in sorted(PROTECTED_STAND_SECRET_NAMES))
    assert "STAND_RUN_TAG" not in PROTECTED_STAND_SECRET_NAMES
    assert "LIVE_CONTOUR" not in PROTECTED_STAND_SECRET_NAMES
    assert "PO_LLM_MODEL" not in PROTECTED_STAND_SECRET_NAMES


def test_admission_canary_is_rejected_outside_the_candidate(tmp_path):
    candidate = tmp_path / "acceptance"
    candidate.mkdir()
    (candidate / "run.log").write_text("safe diagnostic\n", encoding="utf-8")
    never_upload = tmp_path / "never-upload"
    never_upload.mkdir()
    (never_upload / "run.log").write_text("disposable-canary\n", encoding="utf-8")

    assert (
        admit_artifact(
            candidate,
            status_path=tmp_path / "candidate-status.json",
            protected_values=("disposable-canary",),
        )
        is True
    )
    assert (
        admit_artifact(
            never_upload,
            status_path=tmp_path / "canary-status.json",
            protected_values=("disposable-canary",),
        )
        is False
    )


def test_admission_allows_real_stand_preflight_token_refusals_in_handoff_and_final_candidates(
    tmp_path, monkeypatch
):
    """Token diagnostics name credentials but never render their protected values."""
    now = datetime.now(UTC)
    cases = {
        "missing": {},
        "expired": {
            "STAND_CLAUDE_CODE_OAUTH_TOKEN": "opaque-claude-token",
            "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT": (now + timedelta(hours=1)).isoformat(),
            "STAND_CODEX_ACCESS_TOKEN": _jwt_with_expiry(now - timedelta(minutes=1)),
        },
        "near_ttl": {
            "STAND_CLAUDE_CODE_OAUTH_TOKEN": "opaque-claude-token",
            "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT": (now + timedelta(hours=1)).isoformat(),
            "STAND_CODEX_ACCESS_TOKEN": _jwt_with_expiry(now + timedelta(minutes=1)),
        },
    }
    protected_values = protected_values_from_environment(_protected_environment())

    for label, environment in cases.items():
        for name in (
            "STAND_CLAUDE_CODE_OAUTH_TOKEN",
            "STAND_CLAUDE_CODE_OAUTH_TOKEN_EXPIRES_AT",
            "STAND_CODEX_ACCESS_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        refusal = _stand_preflight_refusal()
        handoff = tmp_path / f"handoff-{label}"
        final = tmp_path / f"final-{label}"
        _handoff_candidate(handoff, refusal)
        _final_candidate(final, refusal)

        for candidate in (handoff, final):
            assert admit_artifact(
                candidate,
                status_path=tmp_path / f"{candidate.name}-status.json",
                protected_values=protected_values,
            )


def test_admission_rejects_protected_values_and_assignments_in_handoff_and_final_candidates(
    tmp_path,
):
    protected_values = protected_values_from_environment(_protected_environment())
    protected_value = protected_values[0]

    for candidate_factory, label in ((_handoff_candidate, "handoff"), (_final_candidate, "final")):
        for content, suffix in (
            (protected_value, "bare"),
            (f"BITLAUNCH_API_KEY={protected_value}", "assignment"),
        ):
            candidate = tmp_path / f"{label}-{suffix}"
            status = tmp_path / f"{label}-{suffix}-status.json"
            candidate_factory(candidate, content)

            assert not admit_artifact(
                candidate,
                status_path=status,
                protected_values=protected_values,
            )
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload["status"] == "rejected"
            assert payload["issues"] == [
                {
                    "path": "run/run.log" if label == "handoff" else "run.log",
                    "reason": "candidate contains a supplied protected value",
                }
            ]
            assert protected_value not in status.read_text(encoding="utf-8")


def test_admission_rejects_literal_and_escaped_private_key_pem_in_combination_logs(
    tmp_path,
):
    protected_values = protected_values_from_environment(_protected_environment())
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEfixture\n-----END RSA PRIVATE KEY-----"

    for candidate_factory, label in ((_handoff_candidate, "handoff"), (_final_candidate, "final")):
        for content, suffix in (
            (pem, "literal"),
            (repr(pem), "escaped"),
            (pem.replace("-", r"\u002d").replace("\n", r"\n"), "serialized"),
        ):
            candidate = tmp_path / f"{label}-{suffix}"
            candidate_factory(candidate, "safe diagnostic\n")
            log = candidate / ("run" if label == "handoff" else "") / "claude-codex.log"
            log.write_text(content, encoding="utf-8")
            status = tmp_path / f"{label}-{suffix}-status.json"

            assert not admit_artifact(
                candidate,
                status_path=status,
                protected_values=protected_values,
            )

            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload["status"] == "rejected"
            assert payload["issues"] == [
                {
                    "path": "run/claude-codex.log" if label == "handoff" else "claude-codex.log",
                    "reason": "candidate contains private key material",
                }
            ]
            assert pem not in status.read_text(encoding="utf-8")


def test_admission_refuses_a_missing_protected_environment_value_with_a_named_safe_deficiency(
    tmp_path,
):
    candidate = tmp_path / "candidate"
    status = tmp_path / "status.json"
    _final_candidate(candidate, "value-free diagnostic\n")
    environment = _protected_environment()
    missing_name = sorted(PROTECTED_STAND_SECRET_NAMES)[0]
    environment[missing_name] = ""

    assert not admit_artifact_from_environment(candidate, status_path=status, environ=environment)

    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload == {
        "issues": [
            {
                "name": missing_name,
                "path": None,
                "reason": "protected environment value is missing or empty",
            }
        ],
        "marker": ADMISSION_MARKER,
        "status": "rejected",
    }


def test_rejected_admission_emits_only_safe_reason_and_relative_path_to_the_summary(
    tmp_path, capsys
):
    candidate = tmp_path / "candidate"
    status = tmp_path / "status.json"
    summary = tmp_path / "summary.md"
    canary = "protected-value"
    _final_candidate(candidate, f"BITLAUNCH_API_KEY={canary}\n")

    assert not admit_artifact(candidate, status_path=status, protected_values=(canary,))
    _emit_admission_diagnostics(status, summary)

    diagnostic = capsys.readouterr().out
    assert "run.log: candidate contains a supplied protected value" in diagnostic
    assert summary.read_text(encoding="utf-8") == diagnostic
    assert canary not in diagnostic
