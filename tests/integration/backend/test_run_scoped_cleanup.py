"""A cleanup scoped to one run, against a real daemon, with a neighbour alive.

`test_worker_ownership_labels` proves the labels survive the worker and
`test_run_evidence_by_label` proves the evidence does. This proves the *removal*
does: the live harness's own cleanup module (`tests/live/run_cleanup.py`) is
given nothing but a run id and a docker daemon, and it removes that run's
containers, its QA-egress-shaped sidecar and its dev network — while a second
run's resources, created and left running alongside, are untouched.

Nothing here is reconstructed from a context. Each test creates workers through
the real path — a `CreateWorkerCommand` on `worker:commands`, the real
worker-manager, real containers on the DinD daemon — kills them, deletes the
Redis keys `delete_worker` deletes, and only then hands a run id to the cleanup.
That is the crash case: the harness that made the resources is gone, Redis has
forgotten them, and the labels are all that is left.

The dev network is created here rather than by worker-manager because this suite
runs its workers on host networking (`DOCKER_NETWORK=host`), which is the one
configuration that makes no dev network. That the manager labels the network it
does create is held by
`services/worker-manager/tests/unit/test_worker_ownership.py`; what is held here
is that the label query finds such a network on a real daemon and removes it.
"""

import contextlib
import importlib.util
from pathlib import Path
import sys

import pytest

from shared.contracts.queues.worker import WorkerLabel, WorkerOwnership
from shared.contracts.worker_evidence import removed_worker_evidence_key

from .conftest import REDIS_URL
from .test_run_evidence_by_label import _delete_through_worker_manager, _run_evidence
from .test_worker_ownership_labels import _by_labels, _dead_owned_worker, _fresh_ownership

LIVE_TESTS = Path(__file__).resolve().parents[2] / "live"


def _run_cleanup():
    """The live harness's cleanup module, imported from `tests/live` by path.

    The module the harness and `make test-live-clean` run is the module removing
    things here — not a re-implementation of it that could drift.
    """
    if str(LIVE_TESTS) not in sys.path:
        sys.path.insert(0, str(LIVE_TESTS))
    spec = importlib.util.spec_from_file_location(
        "run_cleanup_for_integration", LIVE_TESTS / "run_cleanup.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sidecar(docker_client, ownership: WorkerOwnership, worker_id: str):
    """A container labelled the way worker-manager labels a QA-egress proxy.

    Built from the run's own worker image, which the daemon already has: this
    suite must not depend on pulling anything, and what matters about a sidecar
    here is its labels, not what it runs.
    """
    image = docker_client.containers.get(f"worker-{worker_id}").image.id
    return docker_client.containers.run(
        image,
        entrypoint=["sleep"],
        command=["300"],
        name=f"qa-egress-{worker_id}",
        detach=True,
        labels={
            WorkerLabel.ID.value: worker_id,
            WorkerLabel.TYPE.value: "qa-egress-proxy",
            **ownership.as_labels(),
        },
    )


def _dev_network(docker_client, ownership: WorkerOwnership, worker_id: str):
    """The `dev_proj_<worker_id>` network, labelled as worker-manager labels it."""
    return docker_client.networks.create(
        f"dev_proj_{worker_id}",
        driver="bridge",
        labels={
            WorkerLabel.ID.value: worker_id,
            WorkerLabel.TYPE.value: "worker-dev-network",
            **ownership.as_labels(),
        },
    )


def _names(resources) -> list[str]:
    return sorted(resource.name for resource in resources)


@pytest.fixture(autouse=True)
def remove_sidecars_and_networks(docker_client):
    """This module's own leftovers, for the paths where a test failed early.

    The suite's shared cleanup removes `worker-*` containers and nothing else,
    which is right for it — the sidecars and dev networks below exist only here.
    """
    yield
    for container in docker_client.containers.list(all=True):
        if container.name.startswith("qa-egress-"):
            with contextlib.suppress(Exception):
                container.remove(force=True)
    for network in docker_client.networks.list():
        if network.name.startswith("dev_proj_"):
            with contextlib.suppress(Exception):
                network.remove()


@pytest.mark.integration
@pytest.mark.asyncio
class TestCleanupFollowsTheRunLabel:
    async def test_a_run_is_removed_whole_from_its_label_alone(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Container, sidecar and network — none of them recorded anywhere."""
        run_cleanup = _run_cleanup()
        ownership = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        _sidecar(docker_client, ownership, worker_id)
        _dev_network(docker_client, ownership, worker_id)

        ops = run_cleanup.docker_sdk_ops(docker_client, REDIS_URL)
        report = run_cleanup.clean_run(ops, ownership.run_id, accounted_workers={worker_id})

        assert sorted(report.removed_containers) == [
            f"qa-egress-{worker_id}",
            f"worker-{worker_id}",
        ]
        assert report.removed_networks == [f"dev_proj_{worker_id}"]
        # The verification the module runs for itself, asked again from outside.
        assert _by_labels(docker_client, **{WorkerLabel.RUN.value: ownership.run_id}) == []
        assert ops.list_networks(ownership.run_id) == []

    async def test_a_neighbouring_run_alive_at_the_same_time_is_untouched(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Both runs on one daemon, and only one of them is cleaned."""
        run_cleanup = _run_cleanup()
        ownership = _fresh_ownership()
        neighbour = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        _dev_network(docker_client, ownership, worker_id)
        neighbour_worker, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, neighbour
        )
        _sidecar(docker_client, neighbour, neighbour_worker)
        _dev_network(docker_client, neighbour, neighbour_worker)

        # Everything on this daemon that answers to no run at all: it must come
        # through a run-scoped cleanup exactly as it went in.
        unowned = {
            container.id
            for container in docker_client.containers.list(all=True)
            if not container.labels.get(WorkerLabel.RUN.value)
        }

        ops = run_cleanup.docker_sdk_ops(docker_client, REDIS_URL)
        run_cleanup.clean_run(ops, ownership.run_id, accounted_workers={worker_id})

        assert _by_labels(docker_client, **{WorkerLabel.RUN.value: ownership.run_id}) == []
        assert _names(_by_labels(docker_client, **{WorkerLabel.RUN.value: neighbour.run_id})) == [
            f"qa-egress-{neighbour_worker}",
            f"worker-{neighbour_worker}",
        ]
        assert _names(ops.list_networks(neighbour.run_id)) == [f"dev_proj_{neighbour_worker}"]
        assert unowned <= {container.id for container in docker_client.containers.list(all=True)}
        # And the neighbour can still be cleaned afterwards, by its own label.
        run_cleanup.clean_run(ops, neighbour.run_id, accounted_workers={neighbour_worker})
        assert _by_labels(docker_client, **{WorkerLabel.RUN.value: neighbour.run_id}) == []

    async def test_cleaning_a_run_twice_leaves_what_cleaning_it_once_left(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """Idempotent on a real daemon: the second pass is a no-op, not an error."""
        run_cleanup = _run_cleanup()
        ownership = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        _dev_network(docker_client, ownership, worker_id)

        ops = run_cleanup.docker_sdk_ops(docker_client, REDIS_URL)
        first = run_cleanup.clean_run(ops, ownership.run_id, accounted_workers={worker_id})
        second = run_cleanup.clean_run(ops, ownership.run_id, accounted_workers={worker_id})

        assert first.removed_containers and first.removed_networks
        assert second.removed_containers == []
        assert second.removed_networks == []
        assert second.errors == []

    async def test_a_retained_worker_name_waits_for_the_runs_evidence(
        self, redis_client, docker_client, scaffolded_workspace, tmp_path
    ):
        """The one Redis key cleanup may not sweep as unexplained residue.

        `delete_worker` keeps `worker:meta:<id>` when a worker's removal record
        could not be stored, because it is then the last thing that can name the
        worker to its run. A cleanup for a run whose evidence has no record of
        that worker keeps the key and says so; the same cleanup deletes it once
        the run's evidence — collected here from that very key — accounts for
        the worker.
        """
        run_cleanup = _run_cleanup()
        run_evidence = _run_evidence()
        ownership = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        # Put the metadata back the way a failed removal-record store leaves it:
        # the container is gone, and the worker's last durable name is not.
        docker_client.containers.get(f"worker-{worker_id}").remove(force=True)
        await redis_client.hset(f"worker:meta:{worker_id}", mapping=ownership.as_redis_meta())

        ops = run_cleanup.docker_sdk_ops(docker_client, REDIS_URL)
        unaccounted = run_cleanup.clean_run(ops, ownership.run_id, accounted_workers=set())

        assert unaccounted.retained_meta == {worker_id: run_cleanup.RETAINED_FOR_EVIDENCE}
        assert unaccounted.errors == []
        assert await redis_client.hexists(f"worker:meta:{worker_id}", "run_id")

        # The capture that accounts for it, taken from the retained name and the
        # removal records — and retained itself before anything else is removed.
        collector = run_evidence.RunEvidenceCollector(
            run_id=ownership.run_id,
            probe=run_evidence.docker_sdk_probe(
                docker_client, run_evidence.redis_removed_workers(REDIS_URL)
            ),
            owned_workers=lambda: ops.meta_workers(ownership.run_id),
        )
        collector.capture()
        retained = run_cleanup.retain_evidence(collector, tmp_path / "evidence.json")

        assert worker_id in run_cleanup.accounted_workers(collector)
        assert retained.exists()

        accounted = run_cleanup.clean_run(
            ops, ownership.run_id, accounted_workers=run_cleanup.accounted_workers(collector)
        )

        assert accounted.deleted_meta == [worker_id]
        assert accounted.retained_meta == {}
        assert await redis_client.hgetall(f"worker:meta:{worker_id}") == {}

    async def test_a_worker_removed_through_the_real_delete_leaves_nothing_to_clean(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """The ordinary path already cleans itself; cleanup after it is a no-op.

        Worth stating: run-scoped cleanup is the recovery path, not a second
        deleter. When `delete_worker` did its job, this finds nothing — and the
        run's removal record, which is evidence and not residue, is still there
        afterwards.
        """
        run_cleanup = _run_cleanup()
        ownership = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        await redis_client.hset(f"worker:meta:{worker_id}", mapping=ownership.as_redis_meta())
        await _delete_through_worker_manager(redis_client, worker_id)

        ops = run_cleanup.docker_sdk_ops(docker_client, REDIS_URL)
        report = run_cleanup.clean_run(ops, ownership.run_id, accounted_workers={worker_id})

        assert report.removed_containers == []
        assert report.errors == []
        assert await redis_client.hexists(removed_worker_evidence_key(ownership.run_id), worker_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unscoped_cleanup_is_refused(docker_client):
    """A cleanup with no run behind it removes nothing at all."""
    run_cleanup = _run_cleanup()
    ops = run_cleanup.docker_sdk_ops(docker_client, REDIS_URL)

    with pytest.raises(run_cleanup.RunCleanupError):
        run_cleanup.clean_run(ops, "", accounted_workers=set())
