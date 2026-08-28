import json

from scripts.stand_acceptance import build_acceptance_artifact, scan_artifact


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
                        "hourly_cost_cents": 42,
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


def test_build_acceptance_artifact_records_observed_lifetime_and_cost(tmp_path):
    manifest, run_dir, cleanup = _write_inputs(tmp_path)
    output = tmp_path / "acceptance"

    complete = build_acceptance_artifact(manifest, run_dir, cleanup, output)

    report = json.loads((output / "final-report.json").read_text(encoding="utf-8"))
    assert complete is True
    assert report["status"] == "complete"
    assert report["machines"][0]["lifetime_seconds"] == 3600
    assert report["machines"][0]["cost_usd"] == "0.42"
    assert report["run_cost_usd"] == "0.42"
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
    assert report["machines"][0]["cost_usd"] is None
    assert report["run_cost_usd"] is None


def test_redaction_scan_rejects_a_supplied_fake_secret_without_echoing_it(tmp_path):
    artifact = tmp_path / "acceptance"
    artifact.mkdir()
    (artifact / "run.log").write_text("fake-secret-value\n", encoding="utf-8")

    errors = scan_artifact(artifact, canaries=("fake-secret-value",))

    assert errors == ["candidate contains a supplied redaction canary"]
