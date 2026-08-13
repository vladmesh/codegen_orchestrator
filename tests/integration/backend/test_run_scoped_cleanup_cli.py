"""The run-scoped cleanup again, driven through the docker CLI it really uses.

`test_run_scoped_cleanup.py` proves the removal rules against a real daemon
through the docker SDK. The path a crash recovery actually takes is not that
one: `scripts/clean_live_tests.py` and `tests/live/pipeline_helpers.py` build
`run_cleanup.docker_cli_ops`, because the live harness drives the stack from the
host and has no docker SDK. That adapter asks the daemon in Go templates and
parses text back, and until this module its templates were only ever answered by
hand-written fixture lines.

Three assumptions in it are the fragile ones, and they are what is asked of a
real daemon here:

* what `{{.Label "com.codegen.run.id"}}` renders when the label is absent —
  an empty field, not a Go `<no value>` placeholder;
* that `{{.Names}}` renders one container's name as one field;
* the `k=v,k=v` shape `_labels_from_pairs` splits `{{.Labels}}` on.

If any of them differed here, the sweep would find nothing and the verification
pass would then report that the run left nothing behind — a false all-clear in
the one check that exists to catch a failed cleanup.

The Redis half of `_DockerCli` is not exercised: it reaches the stack's Redis as
`docker compose exec -T redis redis-cli`, which addresses the live compose
project. This suite's Redis is not in the compose project the runner's docker
CLI talks to — that CLI talks to the DinD daemon, which holds no Redis at all.
So the ops used below are the CLI adapter's docker half, exactly as production
builds it, with the SDK's Redis half in place of the `compose exec` one. The
docker half is what the templates live in.
"""

import contextlib
import dataclasses
from pathlib import Path
from uuid import uuid4

import pytest

from shared.contracts.queues.worker import WorkerLabel

from .conftest import REDIS_URL
from .test_run_scoped_cleanup import _dev_network, _names, _run_cleanup, _sidecar
from .test_worker_ownership_labels import _dead_owned_worker, _fresh_ownership

# The tree as the runner container mounts it; `_DockerCli` runs `docker` there.
REPO_ROOT = Path(__file__).resolve().parents[3]

FIXTURE_PREFIX = "cli-fixture-"


def _ops(run_cleanup, docker_client):
    """The production CLI adapter's docker operations, Redis apart (see module docstring)."""
    cli = run_cleanup.docker_cli_ops(REPO_ROOT)
    over_sdk = run_cleanup.docker_sdk_ops(docker_client, REDIS_URL)
    return dataclasses.replace(
        cli,
        meta_workers=over_sdk.meta_workers,
        delete_keys=over_sdk.delete_keys,
        existing_keys=over_sdk.existing_keys,
    )


def _plain_container(docker_client, worker_id: str, labels: dict[str, str]):
    """A container carrying exactly the given labels, on the run's own image.

    Built from an existing worker's image because this suite must not pull
    anything, and what matters about it here is its labels.
    """
    image = docker_client.containers.get(f"worker-{worker_id}").image.id
    return docker_client.containers.run(
        image,
        entrypoint=["sleep"],
        command=["300"],
        name=f"{FIXTURE_PREFIX}{uuid4().hex[:8]}",
        detach=True,
        labels=labels,
    )


@pytest.fixture(autouse=True)
def remove_this_modules_resources(docker_client):
    """This module's own leftovers, for the paths where a test failed early."""
    yield
    for container in docker_client.containers.list(all=True):
        if container.name.startswith((FIXTURE_PREFIX, "qa-egress-")):
            with contextlib.suppress(Exception):
                container.remove(force=True)
    for network in docker_client.networks.list():
        if network.name.startswith("dev_proj_"):
            with contextlib.suppress(Exception):
                network.remove()


@pytest.mark.integration
@pytest.mark.asyncio
class TestTheCliListingsRenderAndParse:
    async def test_a_run_container_is_listed_with_its_name_and_labels(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """`docker ps` renders one name and each asked-for label, and parses back whole."""
        run_cleanup = _run_cleanup()
        ownership = _fresh_ownership()
        neighbour = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        neighbour_worker, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, neighbour
        )
        # Something on the daemon that answers to no run at all.
        unowned = _plain_container(docker_client, worker_id, labels={})

        listed = _ops(run_cleanup, docker_client).list_containers(ownership.run_id)

        assert listed == [
            run_cleanup.LabelledResource(
                name=f"worker-{worker_id}",
                kind="worker",
                worker_id=worker_id,
                run_id=ownership.run_id,
            )
        ]
        assert unowned.name not in [resource.name for resource in listed]
        assert f"worker-{neighbour_worker}" not in [resource.name for resource in listed]

    async def test_an_absent_label_is_rendered_as_an_empty_field(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """The one rendering a fixture cannot settle: a label the container has not got.

        `_container_from_line` reads the type and worker id straight out of the
        template's fields, and the accounting fence keys on that worker id. A
        placeholder in place of an absent label would make it a worker id.
        """
        run_cleanup = _run_cleanup()
        ownership = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        # The run label, and deliberately none of the others.
        partial = _plain_container(
            docker_client, worker_id, labels={WorkerLabel.RUN.value: ownership.run_id}
        )

        listed = _ops(run_cleanup, docker_client).list_containers(ownership.run_id)

        [resource] = [item for item in listed if item.name == partial.name]
        assert resource == run_cleanup.LabelledResource(
            name=partial.name, kind="", worker_id="", run_id=ownership.run_id
        )

    async def test_the_network_listing_parses_dockers_label_pairs(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """`{{.Labels}}` really is the `k=v,k=v` rendering `_labels_from_pairs` splits."""
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
        _dev_network(docker_client, neighbour, neighbour_worker)

        ops = _ops(run_cleanup, docker_client)

        assert ops.list_networks(ownership.run_id) == [
            run_cleanup.LabelledResource(
                name=f"dev_proj_{worker_id}",
                kind="worker-dev-network",
                worker_id=worker_id,
                run_id=ownership.run_id,
            )
        ]
        assert _names(ops.list_networks(neighbour.run_id)) == [f"dev_proj_{neighbour_worker}"]


@pytest.mark.integration
@pytest.mark.asyncio
class TestCleanupThroughTheCli:
    async def test_a_run_is_removed_whole_while_a_neighbour_is_untouched(
        self, redis_client, docker_client, scaffolded_workspace
    ):
        """The whole run-scoped scenario, over the adapter a crash recovery uses.

        Container, sidecar and network go; the second pass is a no-op rather than
        an error; the neighbouring run, alive on the same daemon throughout, is
        still whole afterwards and can then be cleaned by its own label.
        """
        run_cleanup = _run_cleanup()
        ownership = _fresh_ownership()
        neighbour = _fresh_ownership()

        worker_id, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, ownership
        )
        _sidecar(docker_client, ownership, worker_id)
        _dev_network(docker_client, ownership, worker_id)
        neighbour_worker, _ = await _dead_owned_worker(
            redis_client, docker_client, scaffolded_workspace, neighbour
        )
        _sidecar(docker_client, neighbour, neighbour_worker)
        _dev_network(docker_client, neighbour, neighbour_worker)

        # Everything here that answers to no run: it must come through unchanged.
        unowned = {
            container.id
            for container in docker_client.containers.list(all=True)
            if not container.labels.get(WorkerLabel.RUN.value)
        }

        ops = _ops(run_cleanup, docker_client)
        first = run_cleanup.clean_run(ops, ownership.run_id, accounted_workers={worker_id})
        second = run_cleanup.clean_run(ops, ownership.run_id, accounted_workers={worker_id})

        assert sorted(first.removed_containers) == [
            f"qa-egress-{worker_id}",
            f"worker-{worker_id}",
        ]
        assert first.removed_networks == [f"dev_proj_{worker_id}"]
        assert (second.removed_containers, second.removed_networks, second.errors) == ([], [], [])
        # The verification the module ran for itself, asked again from outside.
        assert ops.list_containers(ownership.run_id) == []
        assert ops.list_networks(ownership.run_id) == []

        assert _names(ops.list_containers(neighbour.run_id)) == [
            f"qa-egress-{neighbour_worker}",
            f"worker-{neighbour_worker}",
        ]
        assert _names(ops.list_networks(neighbour.run_id)) == [f"dev_proj_{neighbour_worker}"]
        assert unowned <= {container.id for container in docker_client.containers.list(all=True)}

        run_cleanup.clean_run(ops, neighbour.run_id, accounted_workers={neighbour_worker})
        assert ops.list_containers(neighbour.run_id) == []
