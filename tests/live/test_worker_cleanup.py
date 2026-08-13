"""How the live harness's teardown reaches this run's workers.

Until card 1185 this file described a manifest-driven teardown: the harness
removed the container its ownership manifest had recorded, and deleted that
worker's Redis keys — `worker:meta` unconditionally among them. Both halves are
now wrong. A run's resources are found by `com.codegen.run.id`, so a worker
nothing recorded is still removed; and `worker:meta` is the last durable name of
a worker whose removal record could not be stored, so it is deleted only once
the run's evidence accounts for it.

What the harness still owns is the wiring, and that is what is tested here: the
run id it scopes cleanup to, the accounting it hands over, and what it does with
a failure. The rules themselves are in `test_run_cleanup.py`.
"""

from types import SimpleNamespace

from live_harness import OwnershipManifest
import pipeline_helpers
from pipeline_helpers import capture_owned_workers, cleanup_owned_workers, find_worker_container
import pytest
from run_cleanup import CleanupOps, LabelledResource

pytestmark = pytest.mark.needs_no_api_credential


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


class _Collector:
    """A run's evidence, reduced to the one question cleanup asks it."""

    def __init__(self, accounted):
        self._accounted = set(accounted)

    def accounted_workers(self):
        return set(self._accounted)


def _context(project_id="project-1", *, accounted=("abc",)):
    manifest = OwnershipManifest("run-1")
    manifest.own("worker", "abc", container="custom-prefix-abc", project_id=project_id)
    return {
        "project_id": project_id,
        "manifest": manifest,
        "run_evidence": _Collector(accounted),
    }


def _ops(*, containers=(), networks=(), meta=(), removed=None, deleted=None):
    """Cleanup operations over a daemon that does exactly as it is told."""
    live_containers = {resource.name: resource for resource in containers}
    live_networks = {resource.name: resource for resource in networks}
    keys: set[str] = set(meta)

    def remove(pool, name):
        pool.pop(name, None)
        if removed is not None:
            removed.append(name)

    def delete(names):
        for name in names:
            keys.discard(name)
        if deleted is not None:
            deleted.extend(names)

    return CleanupOps(
        list_containers=lambda run_id: [
            item for item in live_containers.values() if item.run_id == run_id
        ],
        remove_container=lambda name: remove(live_containers, name),
        list_networks=lambda run_id: [
            item for item in live_networks.values() if item.run_id == run_id
        ],
        remove_network=lambda name: remove(live_networks, name),
        meta_workers=lambda run_id: ["abc"] if meta else [],
        delete_keys=delete,
        existing_keys=lambda names: [name for name in names if name in keys],
    )


def test_cleanup_is_scoped_to_the_runs_label_not_to_the_manifest(monkeypatch):
    """A container the manifest never heard of is still this run's to remove."""
    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", lambda ctx: None)
    removed: list[str] = []
    ops = _ops(
        containers=[
            LabelledResource(
                name="worker-unrecorded", kind="worker", worker_id="xyz", run_id="run-1"
            ),
            LabelledResource(
                name="qa-egress-xyz", kind="qa-egress-proxy", worker_id="xyz", run_id="run-1"
            ),
            LabelledResource(name="worker-neighbour", kind="worker", worker_id="n", run_id="run-2"),
        ],
        networks=[
            LabelledResource(
                name="dev_proj_xyz", kind="worker-dev-network", worker_id="xyz", run_id="run-1"
            )
        ],
        removed=removed,
    )
    errors: list[str] = []

    cleanup_owned_workers(_context(), errors, ops=ops)

    assert errors == []
    assert sorted(removed) == ["dev_proj_xyz", "qa-egress-xyz", "worker-unrecorded"]


def test_a_retained_worker_name_is_deleted_only_when_the_run_accounts_for_it(monkeypatch):
    """The evidence rule, as the harness applies it on the way out."""
    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", lambda ctx: None)
    deleted: list[str] = []
    errors: list[str] = []

    cleanup_owned_workers(
        _context(accounted=()), errors, ops=_ops(meta=["worker:meta:abc"], deleted=deleted)
    )

    assert errors == []
    assert "worker:meta:abc" not in deleted

    deleted.clear()
    cleanup_owned_workers(
        _context(accounted=("abc",)), errors, ops=_ops(meta=["worker:meta:abc"], deleted=deleted)
    )

    assert errors == []
    assert "worker:meta:abc" in deleted


def test_a_cleanup_that_cannot_prove_absence_makes_the_run_red(monkeypatch):
    """Teardown collects the failure; it never swallows it."""
    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", lambda ctx: None)
    stuck = LabelledResource(name="worker-abc", kind="worker", worker_id="abc", run_id="run-1")
    ops = _ops(containers=[stuck])
    ops = CleanupOps(
        list_containers=ops.list_containers,
        remove_container=lambda name: "still exists after removal wait",
        list_networks=ops.list_networks,
        remove_network=ops.remove_network,
        meta_workers=ops.meta_workers,
        delete_keys=ops.delete_keys,
        existing_keys=ops.existing_keys,
    )
    errors: list[str] = []

    cleanup_owned_workers(_context(), errors, ops=ops)

    assert len(errors) == 1
    assert "worker-abc" in errors[0]


def test_an_ownership_refresh_failure_does_not_stop_the_label_cleanup(monkeypatch):
    """The manifest is a second source now, so its failure is not the teardown's."""

    def explode(ctx):
        raise RuntimeError("redis is down")

    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", explode)
    removed: list[str] = []
    ops = _ops(
        containers=[
            LabelledResource(name="worker-abc", kind="worker", worker_id="abc", run_id="run-1")
        ],
        removed=removed,
    )
    errors: list[str] = []

    cleanup_owned_workers(_context(), errors, ops=ops)

    assert errors == ["worker ownership discovery: redis is down"]
    assert removed == ["worker-abc"]
