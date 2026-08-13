"""Run-scoped cleanup, against a daemon and a Redis that only exist in memory.

These are the rules the module holds everywhere: what a run's label selects is
removed, what it does not select is not touched, a second pass is a no-op, and a
`worker:meta` key retained for attribution is deleted only once the run's
evidence accounts for the worker it names.

The same rules against a real daemon, with two runs alive at once, are in
`tests/integration/backend/test_run_scoped_cleanup.py`. What is proved here is
the decision logic; what is proved there is that Docker agrees.
"""

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
import run_cleanup
from run_cleanup import (
    RETAINED_FOR_EVIDENCE,
    CleanupOps,
    LabelledResource,
    RunCleanupError,
    clean_run,
    worker_keys,
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

    report = clean_run(daemon.ops(), RUN, accounted_workers=set())

    assert daemon.keys == {}
    assert report.retained_meta == {}
    assert report.deleted_meta == []


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
