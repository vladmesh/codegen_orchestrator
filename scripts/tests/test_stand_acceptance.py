import json

from scripts.stand_acceptance import (
    ADMISSION_MARKER,
    PROTECTED_STAND_SECRET_NAMES,
    admit_artifact,
    build_acceptance_artifact,
    protected_values_from_environment,
    scan_artifact,
)


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
        "marker": ADMISSION_MARKER,
        "status": "rejected",
    }
    assert "protected-value" not in status_text
    assert "another-value" not in status_text


def test_admission_only_derives_needles_from_explicit_protected_name_allow_list():
    values = protected_values_from_environment(
        {
            "BITLAUNCH_API_KEY": "protected-value",
            "STAND_RUN_TAG": "gha-33168872249-1",
            "LIVE_CONTOUR": "stand",
            "PO_LLM_MODEL": "gpt-public",
        }
    )

    assert values == ("protected-value",)
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
