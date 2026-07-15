from types import SimpleNamespace

from live_harness import OwnershipManifest
import pipeline_helpers
from pipeline_helpers import capture_owned_workers, cleanup_owned_workers, find_worker_container


def test_worker_container_discovery_uses_manager_label(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="custom-prefix-abc\n", stderr="")

    monkeypatch.setattr("pipeline_helpers.subprocess.run", run)

    assert find_worker_container("abc") == "custom-prefix-abc"
    assert "label=com.codegen.worker.id=abc" in calls[0]
    assert "worker-abc" not in calls[0]


def test_worker_container_discovery_has_no_guessed_name_fallback(monkeypatch):
    monkeypatch.setattr(
        "pipeline_helpers.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert find_worker_container("abc") is None


def test_worker_discovery_rejects_mismatched_project_owner(monkeypatch, tmp_path):
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="worker:meta:foreign\n", stderr=""),
            SimpleNamespace(returncode=0, stdout="other-project\n", stderr=""),
        ]
    )
    monkeypatch.setattr(pipeline_helpers.subprocess, "run", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    ctx = _context()
    ctx["manifest"] = OwnershipManifest("run-1")

    capture_owned_workers(ctx)

    assert ctx["manifest"].resources == []


def _context(project_id="project-1"):
    manifest = OwnershipManifest("run-1")
    manifest.own("worker", "abc", container="custom-prefix-abc", project_id=project_id)
    return {"project_id": project_id, "manifest": manifest}


def test_cleanup_accepts_concurrent_removal_only_after_verified_absence(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="removal of container custom-prefix-abc is already in progress",
            )
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")
        if "EXISTS" in cmd:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="6\n", stderr="")

    monkeypatch.setattr("pipeline_helpers.subprocess.run", run)
    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", lambda ctx: None)
    errors = []

    cleanup_owned_workers(_context(), errors, timeout=0, poll_interval=0)

    assert errors == []
    assert any(cmd[:2] == ["docker", "inspect"] for cmd in calls)
    assert any("redis-cli" in cmd and "DEL" in cmd for cmd in calls)


def test_cleanup_stays_red_when_container_remains_and_still_cleans_redis(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="already in progress")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if "EXISTS" in cmd:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="6\n", stderr="")

    monkeypatch.setattr("pipeline_helpers.subprocess.run", run)
    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", lambda ctx: None)
    errors = []

    cleanup_owned_workers(_context(), errors, timeout=0, poll_interval=0)

    assert errors == ["worker abc container custom-prefix-abc: still exists after removal wait"]
    assert any("redis-cli" in cmd and "DEL" in cmd for cmd in calls)


def test_cleanup_reports_operational_inspect_error_safely(monkeypatch):
    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "rm", "-f"]:
            return SimpleNamespace(returncode=0, stdout="custom-prefix-abc", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            return SimpleNamespace(
                returncode=1, stdout="", stderr="daemon unavailable: token=secret"
            )
        if "EXISTS" in cmd:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="6\n", stderr="")

    monkeypatch.setattr("pipeline_helpers.subprocess.run", run)
    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", lambda ctx: None)
    errors = []

    cleanup_owned_workers(_context(), errors, timeout=0, poll_interval=0)

    assert errors == ["worker abc container custom-prefix-abc: Docker inspect failed"]
    assert "secret" not in errors[0]
