from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import clean_live_tests


def _result(stdout="0\n", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


@pytest.mark.parametrize(
    ("project_count", "allocation_count", "worker_keys", "manifest", "label"),
    [
        (1, 0, "", False, "projects=1"),
        (0, 1, "", False, "allocations=1"),
        (0, 0, "worker:meta:w1\n", False, "workers=1"),
        (0, 0, "", True, "ownership_manifests=1"),
    ],
)
def test_verify_no_residue_fails_closed(
    monkeypatch, tmp_path, project_count, allocation_count, worker_keys, manifest, label
):
    calls = iter(
        [_result(f"{project_count}\n"), _result(f"{allocation_count}\n"), _result(worker_keys)]
    )
    monkeypatch.setattr(clean_live_tests, "run_cmd", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    if manifest:
        path = Path(tmp_path) / ".live-manifests" / "run.json"
        path.parent.mkdir()
        path.write_text("{}")

    with pytest.raises(clean_live_tests.CleanupFailure, match=label):
        clean_live_tests.verify_no_residue()


def test_verify_no_residue_accepts_proven_absence(monkeypatch, tmp_path):
    calls = iter([_result(), _result(), _result(stdout="")])
    monkeypatch.setattr(clean_live_tests, "run_cmd", lambda *args, **kwargs: next(calls))
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))

    clean_live_tests.verify_no_residue()


def test_allocation_residue_query_qualifies_project_name(monkeypatch, tmp_path):
    commands = []
    results = iter([_result(), _result(), _result(stdout="")])
    monkeypatch.setattr(
        clean_live_tests,
        "run_cmd",
        lambda cmd, **kwargs: (commands.append(cmd), next(results))[1],
    )
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))

    clean_live_tests.collect_residue_state()

    allocation_sql = commands[1][-1]
    assert "JOIN projects p ON p.id = r.project_id" in allocation_sql
    assert "p.name LIKE" in allocation_sql
    assert " WHERE name LIKE" not in allocation_sql


def test_recover_manifests_removes_proven_orphan(monkeypatch, tmp_path):
    manifest = tmp_path / ".live-manifests" / "orphan.json"
    manifest.parent.mkdir()
    manifest.write_text('{"run_id": "orphan", "resources": []}')
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))

    clean_live_tests.recover_ownership_manifests()
    assert not manifest.exists()


def test_recover_manifests_keeps_unproven_resources(monkeypatch, tmp_path):
    manifest = tmp_path / ".live-manifests" / "run.json"
    manifest.parent.mkdir()
    manifest.write_text(
        '{"run_id":"run","resources":[{"kind":"github_repository","identifier":"org/repo"}]}'
    )
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(
        clean_live_tests,
        "cleanup_manifest_resources",
        lambda data: ["github_repository org/repo"],
    )

    with pytest.raises(clean_live_tests.CleanupFailure, match="github_repository org/repo"):
        clean_live_tests.recover_ownership_manifests()
    assert manifest.exists()
