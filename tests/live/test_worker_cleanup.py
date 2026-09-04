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

import json
from types import SimpleNamespace

from live_harness import OwnershipManifest
import pipeline_helpers
from pipeline_helpers import capture_owned_workers, cleanup_owned_workers, find_worker_container
import pytest
from run_cleanup import CleanupOps, LabelledResource
import run_evidence
from run_evidence import ContainerProbe, RunEvidenceCollector, WorkerRole

pytestmark = pytest.mark.needs_no_api_credential


@pytest.fixture(autouse=True)
def evidence_under_tmp(monkeypatch, tmp_path):
    """Teardown retains its evidence artifact; keep that write out of the checkout."""
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)
    return tmp_path / ".live-manifests" / "evidence" / "run-1.json"


def test_worker_container_discovery_uses_manager_label(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="custom-prefix-abc\n", stderr="")

    monkeypatch.setattr("pipeline_helpers.subprocess.run", run)

    assert find_worker_container("abc") == "custom-prefix-abc"
    assert "label=com.codegen.worker.id=abc" in calls[0]
    assert "label=com.codegen.type=worker" in calls[0]
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


def _no_containers() -> ContainerProbe:
    """A probe for a run whose containers are all gone: the crash case."""
    return ContainerProbe(
        list_run_workers=lambda run_id: [],
        inspect=lambda container: None,
        logs=lambda container, tail: None,
        removed_workers=lambda run_id: [],
    )


def _collector(accounted, *, probe=None):
    """A real run-evidence collector already holding a record for each worker.

    The stub this replaced answered `accounted_workers` and nothing else, which
    is why it could not catch either half of the rule it was standing in for:
    cleanup now also asks the collector to record a miss for a worker its capture
    could not read, and retains what it holds before removing anything.
    """
    collector = RunEvidenceCollector(run_id="run-1", probe=probe or _no_containers())
    for worker_id in accounted:
        collector.observe_absent(
            worker_id, WorkerRole.DEVELOPER, "read by an earlier pass of this run"
        )
    return collector


def _context(project_id="project-1", *, accounted=("abc",), collector=None):
    manifest = OwnershipManifest("run-1")
    manifest.own("worker", "abc", container="custom-prefix-abc", project_id=project_id)
    return {
        "project_id": project_id,
        "manifest": manifest,
        "run_evidence": collector if collector is not None else _collector(accounted),
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


def test_recovery_keeps_naming_the_worker_after_its_whole_path_has_run(monkeypatch, tmp_path):
    """The retained name survives the pass that runs after it was deleted.

    The scenario is the one `delete_worker` leaves behind: a crashed run whose
    worker's removal record could not be stored, so `worker:meta:<id>` is the
    last thing naming the worker to its run. Recovery makes two passes over that
    manifest — the run-scoped label sweep in `clean_live_tests`, which records
    the worker and then deletes the metadata its record accounts for, and the
    manifest round-trip afterwards, which reaches `cleanup_owned_workers` with no
    collector of its own and therefore knows nothing at all by then. Both write
    the run's one evidence artifact, and the second must not be able to unsay the
    first: the accounting that authorised a deletion cannot be erased by that
    deletion's own successor.
    """
    from scripts import clean_live_tests

    monkeypatch.setattr("pipeline_helpers.capture_owned_workers", lambda ctx: None)
    monkeypatch.setattr(run_evidence, "docker_probe", lambda root=None: _no_containers())
    monkeypatch.setattr(clean_live_tests, "ORCHESTRATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(pipeline_helpers, "ORCHESTRATOR_ROOT", tmp_path)

    keys = {"worker:meta:abc"}
    ops = CleanupOps(
        list_containers=lambda run_id: [],
        remove_container=lambda name: None,
        list_networks=lambda run_id: [],
        remove_network=lambda name: None,
        meta_workers=lambda run_id: ["abc"] if "worker:meta:abc" in keys else [],
        delete_keys=keys.difference_update,
        existing_keys=lambda names: [name for name in names if name in keys],
    )
    monkeypatch.setattr("run_cleanup.docker_cli_ops", lambda root, **kwargs: ops)

    assert clean_live_tests.cleanup_run_scoped_resources("run-1") == []
    assert keys == set()  # the retained name was accounted for, and only then deleted

    artifact = tmp_path / ".live-manifests" / "evidence" / "run-1.json"
    assert [record["worker_id"] for record in json.loads(artifact.read_text())["workers"]] == [
        "abc"
    ]

    # The manifest round-trip, reaching the same run through `cleanup_all`.
    errors: list[str] = []
    ctx = {"project_id": "project-1", "manifest": OwnershipManifest("run-1")}
    cleanup_owned_workers(ctx, errors, ops=ops)

    assert errors == []
    retained = json.loads(artifact.read_text())
    assert [record["worker_id"] for record in retained["workers"]] == ["abc"]
    assert retained["passes"] == 2


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
