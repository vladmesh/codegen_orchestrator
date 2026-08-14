"""Run-scoped cleanup, against a daemon and a Redis that only exist in memory.

These are the rules the module holds everywhere: what a run's label selects is
removed, what it does not select is not touched, a second pass is a no-op, a
`worker:meta` key retained for attribution is deleted only once the run's
evidence accounts for the worker it names, and nothing labelled is removed at all
until that evidence names its worker.

The same rules against a real daemon, with two runs alive at once, are in
`tests/integration/backend/test_run_scoped_cleanup.py`. What is proved here is
the decision logic; what is proved there is that Docker agrees.
"""

from dataclasses import dataclass, field
import json
from types import SimpleNamespace

import pytest
import run_cleanup
from run_cleanup import (
    RETAINED_FOR_EVIDENCE,
    CleanupOps,
    LabelledResource,
    RunCleanupError,
    account_listed_workers,
    accounted_workers,
    clean_run,
    merge_worker_records,
    retain_evidence,
    worker_keys,
)
from run_evidence import (
    CaptureStatus,
    ContainerProbe,
    ListedWorker,
    RunEvidenceCollector,
    WorkerRole,
)

from shared.contracts.queues.worker import WorkerLabel

pytestmark = pytest.mark.needs_no_api_credential

RUN = "live-first"
NEIGHBOUR = "live-second"


def _container(name: str, run_id: str, worker_id: str, kind: str = "worker") -> LabelledResource:
    return LabelledResource(name=name, kind=kind, worker_id=worker_id, run_id=run_id)


@dataclass
class FakeDaemon:
    """Containers, networks and Redis keys, with the run label as the only index."""

    containers: dict[str, LabelledResource] = field(default_factory=dict)
    networks: dict[str, LabelledResource] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    # worker id -> the run its `worker:meta` names
    meta: dict[str, str] = field(default_factory=dict)
    stuck: set[str] = field(default_factory=set)
    listed_labels: list[str] = field(default_factory=list)

    def own(self, run_id: str, worker_id: str, *, network: bool = True, proxy: bool = False):
        self.containers[f"worker-{worker_id}"] = _container(
            f"worker-{worker_id}", run_id, worker_id
        )
        if proxy:
            self.containers[f"qa-egress-{worker_id}"] = _container(
                f"qa-egress-{worker_id}", run_id, worker_id, kind="qa-egress-proxy"
            )
        if network:
            self.networks[f"dev_proj_{worker_id}"] = LabelledResource(
                name=f"dev_proj_{worker_id}",
                kind="worker-dev-network",
                worker_id=worker_id,
                run_id=run_id,
            )
        self.meta[worker_id] = run_id
        self.keys[f"worker:meta:{worker_id}"] = run_id
        for key in worker_keys(worker_id):
            self.keys[key] = run_id

    def unlabelled(self, name: str) -> None:
        """A long-lived service container: no ownership labels of any kind."""
        self.containers[name] = LabelledResource(name=name, kind="", worker_id="", run_id="")

    def ops(self) -> CleanupOps:
        def selected(pool: dict[str, LabelledResource], run_id: str):
            self.listed_labels.append(f"{WorkerLabel.RUN.value}={run_id}")
            return [item for item in pool.values() if item.run_id == run_id]

        def remove(pool: dict[str, LabelledResource], name: str) -> str | None:
            if name in self.stuck:
                return "still exists after removal wait"
            pool.pop(name, None)
            return None

        def delete_keys(keys: list[str]) -> None:
            for key in keys:
                self.keys.pop(key, None)
                if key.startswith("worker:meta:"):
                    self.meta.pop(key.removeprefix("worker:meta:"), None)

        return CleanupOps(
            list_containers=lambda run_id: selected(self.containers, run_id),
            remove_container=lambda name: remove(self.containers, name),
            list_networks=lambda run_id: selected(self.networks, run_id),
            remove_network=lambda name: remove(self.networks, name),
            meta_workers=lambda run_id: [
                worker_id for worker_id, owner in self.meta.items() if owner == run_id
            ],
            delete_keys=delete_keys,
            existing_keys=lambda keys: [key for key in keys if key in self.keys],
        )


def test_a_run_is_cleaned_from_its_label_alone():
    """Containers, the QA sidecar beside them and the dev network under them."""
    daemon = FakeDaemon()
    daemon.own(RUN, "dev-1")
    daemon.own(RUN, "qa-1", proxy=True)

    report = clean_run(daemon.ops(), RUN, accounted_workers={"dev-1", "qa-1"})

    assert sorted(report.removed_containers) == ["qa-egress-qa-1", "worker-dev-1", "worker-qa-1"]
    assert sorted(report.removed_networks) == ["dev_proj_dev-1", "dev_proj_qa-1"]
    assert daemon.containers == {}
    assert daemon.networks == {}
    assert daemon.keys == {}


def test_a_neighbouring_run_and_the_services_survive():
    """The label is the fence as well as the finder."""
    daemon = FakeDaemon()
    daemon.own(RUN, "dev-1")
    daemon.own(NEIGHBOUR, "dev-2", proxy=True)
    daemon.unlabelled("codegen-worker-manager-1")
    daemon.unlabelled("codegen-redis-1")

    clean_run(daemon.ops(), RUN, accounted_workers={"dev-1"})

    assert sorted(daemon.containers) == [
        "codegen-redis-1",
        "codegen-worker-manager-1",
        "qa-egress-dev-2",
        "worker-dev-2",
    ]
    assert sorted(daemon.networks) == ["dev_proj_dev-2"]
    assert daemon.meta == {"dev-2": NEIGHBOUR}
    assert "worker:meta:dev-2" in daemon.keys
    assert all(label == f"{WorkerLabel.RUN.value}={RUN}" for label in daemon.listed_labels)


def test_running_it_twice_leaves_what_running_it_once_left():
    """Idempotent, and the second pass is not an error."""
    daemon = FakeDaemon()
    daemon.own(RUN, "dev-1", proxy=True)

    first = clean_run(daemon.ops(), RUN, accounted_workers={"dev-1"})
    state = (dict(daemon.containers), dict(daemon.networks), dict(daemon.keys))
    second = clean_run(daemon.ops(), RUN, accounted_workers={"dev-1"})

    assert first.removed_containers
    assert second.removed_containers == []
    assert second.removed_networks == []
    assert second.errors == []
    assert (daemon.containers, daemon.networks, daemon.keys) == state


def test_a_container_that_will_not_go_away_fails_loudly():
    """Verification is the point: a cleanup that did not clean must be red."""
    daemon = FakeDaemon()
    daemon.own(RUN, "dev-1")
    daemon.stuck.add("worker-dev-1")

    with pytest.raises(RunCleanupError) as failure:
        clean_run(daemon.ops(), RUN, accounted_workers={"dev-1"})

    assert "worker-dev-1" in str(failure.value)
    assert f"containers remain for run {RUN}" in str(failure.value)


def test_a_resource_labelled_another_run_is_refused_not_removed():
    """A listing that answers with a neighbour is a defect, not an instruction."""
    daemon = FakeDaemon()
    daemon.own(NEIGHBOUR, "dev-2")
    ops = daemon.ops()
    mislabelled = CleanupOps(
        list_containers=lambda run_id: list(daemon.containers.values()),
        remove_container=ops.remove_container,
        list_networks=lambda run_id: [],
        remove_network=ops.remove_network,
        meta_workers=lambda run_id: [],
        delete_keys=ops.delete_keys,
        existing_keys=ops.existing_keys,
    )

    with pytest.raises(RunCleanupError) as failure:
        clean_run(mislabelled, RUN, accounted_workers=set())

    assert f"labelled run {NEIGHBOUR!r}" in str(failure.value)
    assert "worker-dev-2" in daemon.containers


def test_a_retained_worker_name_outlives_a_run_with_no_evidence_for_it():
    """The expected residue of a failed removal record is not swept as an anomaly."""
    daemon = FakeDaemon()
    daemon.own(RUN, "dev-1")
    daemon.containers.clear()
    daemon.networks.clear()

    report = clean_run(daemon.ops(), RUN, accounted_workers=set())

    assert report.retained_meta == {"dev-1": RETAINED_FOR_EVIDENCE}
    assert daemon.keys == {"worker:meta:dev-1": RUN}
    assert report.errors == []


def test_the_retained_name_is_removed_once_the_run_accounts_for_the_worker():
    """Accounted for means the run's evidence holds a record for it."""
    daemon = FakeDaemon()
    daemon.own(RUN, "dev-1")
    daemon.containers.clear()
    daemon.networks.clear()

    report = clean_run(daemon.ops(), RUN, accounted_workers={"dev-1"})

    assert report.deleted_meta == ["dev-1"]
    assert report.retained_meta == {}
    assert daemon.keys == {}


def test_the_stray_keys_of_a_worker_with_no_name_left_are_still_removed():
    """A worker whose metadata is already deleted is found by its container."""
    daemon = FakeDaemon()
    daemon.own(RUN, "dev-1")
    # What `delete_worker` leaves when it got as far as deleting `worker:meta`
    # and no further: the name is gone, and keys under it are not.
    daemon.meta.pop("dev-1")
    daemon.keys.pop("worker:meta:dev-1")

    report = clean_run(daemon.ops(), RUN, accounted_workers={"dev-1"})

    assert daemon.keys == {}
    assert report.retained_meta == {}
    assert report.deleted_meta == []


def _probe(listed, *, inspect, removed=()):
    """A run-evidence probe over containers this test decides the fate of."""
    return ContainerProbe(
        list_run_workers=lambda run_id: [
            ListedWorker(container=f"worker-{worker_id}", worker_id=worker_id, ownership={})
            for worker_id in listed
        ],
        inspect=inspect,
        logs=lambda container, tail: "",
        removed_workers=lambda run_id: list(removed),
    )


def _unreadable(container: str):
    """The transient, non-NotFound docker failure a capture cannot recover from."""
    raise RuntimeError(f"docker inspect {container} failed: daemon temporarily unavailable")


class TestRemovalIsFencedByAccounting:
    """A capture that failed is not a licence to remove what it failed to read."""

    def test_a_listed_worker_the_evidence_cannot_name_is_kept_and_the_cleanup_is_red(self):
        """The container is that worker's last attribution; it outlives the cleanup."""
        daemon = FakeDaemon()
        daemon.own(RUN, "dev-1", proxy=True)

        with pytest.raises(RunCleanupError) as failure:
            clean_run(daemon.ops(), RUN, accounted_workers=set())

        assert "no record for worker 'dev-1'" in str(failure.value)
        assert sorted(daemon.containers) == ["qa-egress-dev-1", "worker-dev-1"]
        assert sorted(daemon.networks) == ["dev_proj_dev-1"]
        assert daemon.keys[f"worker:meta:{'dev-1'}"] == RUN
        assert daemon.keys[f"worker:status:{'dev-1'}"] == RUN

    def test_one_workers_missing_record_does_not_hold_up_an_accounted_one(self):
        """The fence is per worker: what is named is still removed."""
        daemon = FakeDaemon()
        daemon.own(RUN, "dev-1")
        daemon.own(RUN, "dev-2")

        with pytest.raises(RunCleanupError):
            clean_run(daemon.ops(), RUN, accounted_workers={"dev-2"})

        assert sorted(daemon.containers) == ["worker-dev-1"]
        assert sorted(daemon.networks) == ["dev_proj_dev-1"]

    def test_a_capture_that_failed_becomes_a_named_miss_and_then_a_removal(self):
        """The reviewer's scenario: the listing succeeds and one inspect does not.

        The worker is not silently removed and it does not fence the teardown
        forever either. It is written into the artifact as a missed capture that
        says why its ending could not be read — an acceptable ending — and only
        that record authorises the removal.
        """
        daemon = FakeDaemon()
        daemon.own(RUN, "dev-1")
        ops = daemon.ops()
        collector = RunEvidenceCollector(
            run_id=RUN, probe=_probe(["dev-1"], inspect=_unreadable), owned_workers=list
        )
        collector.capture()

        assert accounted_workers(collector) == set()
        assert any("capture failed" in error for error in collector.errors)

        assert account_listed_workers(collector, ops, RUN) == ["dev-1"]

        [record] = collector.records()
        assert record["worker_id"] == "dev-1"
        assert record["exit_code"]["status"] == CaptureStatus.MISSED.value
        assert "daemon temporarily unavailable" in record["exit_code"]["reason"]

        report = clean_run(ops, RUN, accounted_workers=accounted_workers(collector))

        assert report.fenced == []
        assert report.removed_containers == ["worker-dev-1"]
        assert daemon.containers == {}

    def test_a_worker_already_read_is_not_downgraded_to_a_miss(self):
        """Accounting adds names; it never restates a known ending as an unknown one."""
        daemon = FakeDaemon()
        daemon.own(RUN, "dev-1")
        inspected = {
            "Config": {"Env": ["WORKER_TYPE=developer"], "Image": "worker:test"},
            "Image": "sha256:abc",
            "Created": "2026-08-13T00:00:00Z",
            "Mounts": [],
            "State": {
                "Running": False,
                "Status": "exited",
                "OOMKilled": False,
                "StartedAt": "2026-08-13T00:00:00Z",
                "FinishedAt": "2026-08-13T00:00:10Z",
                "Error": "",
                "ExitCode": 3,
            },
        }
        collector = RunEvidenceCollector(
            run_id=RUN,
            probe=_probe(["dev-1"], inspect=lambda container: inspected),
            owned_workers=list,
        )
        collector.capture()

        assert account_listed_workers(collector, daemon.ops(), RUN) == []
        [record] = collector.records()
        assert record["exit_code"] == {
            "status": CaptureStatus.CAPTURED.value,
            "value": 3,
            "reason": None,
        }


class TestTheArtifactOnlyEverGains:
    """One run has one evidence artifact, and no pass over it can know less."""

    def _artifact(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def _record(self, worker_id: str, *, exit_code) -> dict:
        """One worker record in the shape the collector really writes them."""
        collector = RunEvidenceCollector(run_id=RUN, probe=_probe([], inspect=_unreadable))
        collector.observe_absent(worker_id, WorkerRole.DEVELOPER, "the ending was not read")
        [record] = collector.records()
        if exit_code is not None:
            record["exit_code"] = {
                "status": CaptureStatus.CAPTURED.value,
                "value": exit_code,
                "reason": None,
            }
        return record

    def test_the_spelling_of_a_captured_status_is_one_spelling(self):
        """The merge reads the artifact's JSON; this holds it to the collector's word."""
        assert run_cleanup.CAPTURED == CaptureStatus.CAPTURED.value

    def test_a_later_poorer_pass_cannot_unname_a_worker(self, tmp_path):
        """The defect: the second pass runs after the sources are gone."""
        path = tmp_path / "evidence.json"
        first = RunEvidenceCollector(
            run_id=RUN,
            probe=_probe([], inspect=lambda container: None),
            owned_workers=lambda: ["dev-1"],
        )
        first.capture()
        retain_evidence(first, path)

        assert [record["worker_id"] for record in self._artifact(path)["workers"]] == ["dev-1"]

        # The manifest round-trip's own collector: container, removal record and
        # `worker:meta` are all gone by now, so it knows about nothing at all.
        second = RunEvidenceCollector(
            run_id=RUN, probe=_probe([], inspect=lambda container: None), owned_workers=list
        )
        second.capture()
        retain_evidence(second, path)

        artifact = self._artifact(path)
        assert [record["worker_id"] for record in artifact["workers"]] == ["dev-1"]
        assert artifact["passes"] == 2

    def test_a_later_richer_pass_fills_a_record_in(self):
        """Merging is not "first writer wins" either: a real ending replaces a miss."""
        merged = merge_worker_records(
            [self._record("dev-1", exit_code=None)], [self._record("dev-1", exit_code=137)]
        )

        assert [record["exit_code"]["value"] for record in merged] == [137]

    def test_capture_errors_of_every_pass_are_kept(self, tmp_path):
        path = tmp_path / "evidence.json"
        first = RunEvidenceCollector(run_id=RUN, probe=_probe([], inspect=_unreadable))
        first.note_error("first pass: redis unreachable")
        retain_evidence(first, path)
        second = RunEvidenceCollector(run_id=RUN, probe=_probe([], inspect=_unreadable))
        second.note_error("second pass: docker unreachable")
        retain_evidence(second, path)

        assert self._artifact(path)["capture_errors"] == [
            "first pass: redis unreachable",
            "second pass: docker unreachable",
        ]

    def test_another_runs_artifact_is_never_written_over(self, tmp_path):
        path = tmp_path / "evidence.json"
        retain_evidence(
            RunEvidenceCollector(run_id=NEIGHBOUR, probe=_probe([], inspect=_unreadable)), path
        )

        with pytest.raises(RunCleanupError, match=NEIGHBOUR):
            retain_evidence(
                RunEvidenceCollector(run_id=RUN, probe=_probe([], inspect=_unreadable)), path
            )

    def test_an_unreadable_artifact_is_not_replaced(self, tmp_path):
        """What cannot be read cannot be merged, and so must not be overwritten."""
        path = tmp_path / "evidence.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(RunCleanupError, match="cannot be read"):
            retain_evidence(
                RunEvidenceCollector(run_id=RUN, probe=_probe([], inspect=_unreadable)), path
            )
        assert path.read_text(encoding="utf-8") == "{not json"


def test_an_unscoped_cleanup_is_refused():
    """There is no run whose label is everybody's."""
    with pytest.raises(RunCleanupError):
        clean_run(FakeDaemon().ops(), "", accounted_workers=set())


class TestTheDockerCliOperations:
    """What the harness's real operations make of the docker CLI's answers."""

    def _ops(self, monkeypatch, respond, tmp_path):
        monkeypatch.setattr(run_cleanup.subprocess, "run", respond)
        monkeypatch.setattr(run_cleanup.time, "sleep", lambda _seconds: None)
        return run_cleanup.docker_cli_ops(tmp_path, timeout=0, poll_interval=0)

    def test_the_run_label_is_the_only_filter_asked_for(self, monkeypatch, tmp_path):
        seen = []

        def respond(cmd, **kwargs):
            seen.append(cmd)
            return SimpleNamespace(
                returncode=0,
                stdout="worker-dev-1\tdev-1\tworker\tlive-first\n",
                stderr="",
            )

        ops = self._ops(monkeypatch, respond, tmp_path)

        assert ops.list_containers(RUN) == [_container("worker-dev-1", RUN, "dev-1")]
        assert f"label={WorkerLabel.RUN.value}={RUN}" in seen[0]

    def test_a_networks_labels_are_read_back_off_the_network(self, monkeypatch, tmp_path):
        labels = ",".join(
            [
                f"{WorkerLabel.TYPE.value}=worker-dev-network",
                f"{WorkerLabel.ID.value}=dev-1",
                f"{WorkerLabel.RUN.value}={RUN}",
            ]
        )

        def respond(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=f"dev_proj_dev-1\t{labels}\n", stderr="")

        ops = self._ops(monkeypatch, respond, tmp_path)

        assert ops.list_networks(RUN) == [
            LabelledResource(
                name="dev_proj_dev-1",
                kind="worker-dev-network",
                worker_id="dev-1",
                run_id=RUN,
            )
        ]

    def test_a_concurrent_removal_is_accepted_only_after_verified_absence(
        self, monkeypatch, tmp_path
    ):
        def respond(cmd, **kwargs):
            if cmd[:3] == ["docker", "rm", "-f"]:
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="removal of worker-dev-1 is already in progress"
                )
            return SimpleNamespace(returncode=1, stdout="", stderr="No such container")

        ops = self._ops(monkeypatch, respond, tmp_path)

        assert ops.remove_container("worker-dev-1") is None

    def test_a_container_still_there_after_the_wait_is_a_failure(self, monkeypatch, tmp_path):
        def respond(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        ops = self._ops(monkeypatch, respond, tmp_path)

        assert ops.remove_container("worker-dev-1") == "still exists after removal wait"

    def test_a_removed_network_is_absent_in_the_daemons_own_wording(self, monkeypatch, tmp_path):
        """A network gone is `network <name> not found`, never `no such object`.

        Reading that wording as a failure made every removed `dev_proj_*` network
        end a live run in a cleanup error, with the network already gone.
        """

        def respond(cmd, **kwargs):
            if cmd[:3] == ["docker", "network", "rm"]:
                return SimpleNamespace(returncode=0, stdout="dev_proj_dev-1\n", stderr="")
            return SimpleNamespace(
                returncode=1,
                stdout="[]\n",
                stderr="Error response from daemon: network dev_proj_dev-1 not found",
            )

        ops = self._ops(monkeypatch, respond, tmp_path)

        assert ops.remove_network("dev_proj_dev-1") is None

    def test_a_network_removed_by_someone_else_is_accepted(self, monkeypatch, tmp_path):
        """`docker network rm` refuses a network already gone; that is not a failure."""

        def respond(cmd, **kwargs):
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Error response from daemon: network dev_proj_dev-1 not found",
            )

        ops = self._ops(monkeypatch, respond, tmp_path)

        assert ops.remove_network("dev_proj_dev-1") is None

    def test_a_failure_naming_another_network_is_not_read_as_absence(self, monkeypatch, tmp_path):
        """Absence is claimed for the network asked about, not for any `not found`."""

        def respond(cmd, **kwargs):
            if cmd[:3] == ["docker", "network", "rm"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Error response from daemon: network dev_proj_other not found",
            )

        ops = self._ops(monkeypatch, respond, tmp_path)

        assert ops.remove_network("dev_proj_dev-1") == "docker inspect failed"

    def test_an_operational_failure_is_reported_without_quoting_the_daemon(
        self, monkeypatch, tmp_path
    ):
        """A failed inspect can quote an environment; the reason must be safe."""

        def respond(cmd, **kwargs):
            if cmd[:3] == ["docker", "rm", "-f"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=1, stdout="", stderr="daemon unavailable: token=s3cr3t"
            )

        ops = self._ops(monkeypatch, respond, tmp_path)

        reason = ops.remove_container("worker-dev-1")
        assert reason == "docker inspect failed"
        assert "s3cr3t" not in reason
