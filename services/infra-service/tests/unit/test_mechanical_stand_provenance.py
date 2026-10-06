"""The rsynced stand has released source files but no Git metadata."""

import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

SOURCE_SHA = "a9628c80d748ac3ec72bd16814d3a9016ae08c7e"


@pytest.fixture
def stand(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "tests/live"))
    import mechanical_install

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STAND_SOURCE_SHA", SOURCE_SHA)
    monkeypatch.setenv("STAND_SERVICE_RELEASE_COMPOSE", "released.compose.yml")
    record = {
        "schema_version": 1,
        "git_sha": SOURCE_SHA,
        "source_hash": "stand-source-hash",
        "images": {
            "api": {"reference": "ghcr.io/owner/api@sha256:" + "a" * 64},
        },
    }
    (tmp_path / "deployed-service-images.json").write_text(json.dumps(record))
    return mechanical_install, record


@pytest.mark.subprocess
def test_service_provenance_checks_the_release_without_git(stand, monkeypatch):
    probe, record = stand
    calls = []

    def check_output(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "docker", "stand source identity must not require .git"
        return "{}"

    monkeypatch.setattr(probe.subprocess, "check_output", check_output)
    monkeypatch.setattr(probe.service_release, "readback", lambda **kwargs: [])
    assert probe.service_provenance() == record
    assert calls


def test_service_provenance_refuses_another_revisions_release(stand, monkeypatch):
    probe, _ = stand
    monkeypatch.setenv("STAND_SOURCE_SHA", "b" * 40)
    with pytest.raises(probe.h.Level1PhaseFailed, match="exact"):
        probe.service_provenance()


def test_service_provenance_still_refuses_failed_container_readback(stand, monkeypatch):
    probe, _ = stand
    monkeypatch.setattr(probe.subprocess, "check_output", lambda *args, **kwargs: "{}")
    monkeypatch.setattr(
        probe.service_release, "readback", lambda **kwargs: ["running image digest differs"]
    )
    with pytest.raises(probe.h.Level1PhaseFailed, match="running image digest differs"):
        probe.service_provenance()


@pytest.mark.parametrize("source", [None, "", "main", "a9628c80d748", "g" * 40])
def test_service_provenance_requires_a_full_workflow_revision(stand, monkeypatch, source):
    probe, _ = stand
    if source is None:
        monkeypatch.delenv("STAND_SOURCE_SHA")
    else:
        monkeypatch.setenv("STAND_SOURCE_SHA", source)
    with pytest.raises(probe.h.Level1PhaseFailed, match="STAND_SOURCE_SHA"):
        probe.service_provenance()


@pytest.mark.parametrize("source", [SOURCE_SHA, None])
def test_partial_failure_artifact_survives_a_gitless_stand(stand, monkeypatch, tmp_path, source):
    probe, _ = stand
    if source is None:
        monkeypatch.delenv("STAND_SOURCE_SHA")
    monkeypatch.setattr(probe, "evidence_output_directory", lambda: tmp_path / "evidence")
    ctx = {
        "mechanical_acceptance": {"status": "failed", "phase": "service_provenance"},
        "mechanical_started": time.monotonic(),
        "manifest": SimpleNamespace(run_id="stand-test"),
    }
    artifact = json.loads(probe.write_artifact(ctx).read_text())
    assert artifact["status"] == "failed"
    assert artifact["phase"] == "service_provenance"
    assert artifact["source_sha"] == source
    if source is None:
        assert "STAND_SOURCE_SHA" in artifact["source_sha_error"]
