"""What a QA executor's real broker token can and cannot make the host do.

The QA executor's token is not a secret from the agent inside its container: the
CLI runs as the same user as the wrapper that holds it, so `/proc/<ppid>/environ`
hands it over. This test therefore assumes the agent has the token and asks the
only question that matters — what the token is worth.

Nothing here is stubbed. A real worker credential is issued through
`WorkerManager._register_broker_worker`, the same call worker creation makes; it
is presented to the real broker and to the real worker-manager over HTTP; and
the observable is the management host's own Docker daemon.

The test carries its positive control, because a refusal proves nothing if the
capability was never there:

* a developer worker, with an identical workspace and the identical request,
  really does cause `docker compose build` to run on the management daemon — the
  image exists afterwards and the container built from it carries a marker that
  only a `RUN` instruction executed on that daemon could have written;
* the QA worker, differing only in the type the server recorded for it, is
  refused at both boundaries and leaves no such image behind;
* the QA worker's own turn protocol still works, so the boundary is a boundary
  and not an outage.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets

import docker
import httpx
import pytest
from redis.asyncio import Redis

from shared.contracts.vocab import WorkerType
from src.compose_validator import RESOURCE_IDENTITY_POLICY
from src.manager import WorkerManager

BROKER_URL = os.environ["WORKER_BROKER_URL"].rstrip("/")
WORKER_MANAGER_URL = os.environ["WORKER_MANAGER_URL"].rstrip("/")
REDIS_URL = os.environ["REDIS_URL"]
WORKSPACES = os.environ["SCAFFOLDED_WORKSPACE_PATH"]

# Present on the management daemon because this compose file built it. A build
# `FROM` it needs no registry, so the check does not depend on network access.
BUILD_BASE_IMAGE = os.environ.get("QA_EGRESS_TEST_IMAGE", "codegen-orchestrator/worker-manager:test")

SERVICE_NAME = "probe"
BUILD_TIMEOUT_SECONDS = 300


def _build_image_tag(worker_id: str) -> str:
    """What worker-manager names an image it builds for a worker."""
    return RESOURCE_IDENTITY_POLICY.build_image(worker_id, SERVICE_NAME)


def _write_buildable_project(worker_id: str, marker: str) -> str:
    """A workspace an agent could have written: one service, built from a Dockerfile.

    The `RUN` instruction is the point. It is ordinary Docker: whatever it says
    executes on the management host's builder, on the builder's network, outside
    the QA executor's internal network and its proxy. Here it writes a marker so
    the test can tell execution from mere image resolution; in the reachable
    attack it would be a request to the deployment under test.
    """
    workspace = os.path.join(WORKSPACES, worker_id)
    infra = os.path.join(workspace, "infra")
    os.makedirs(infra, exist_ok=True)
    with open(os.path.join(workspace, "Dockerfile"), "w") as handle:
        # `/tmp` because the base image runs as a non-root user; where the file
        # lands is irrelevant, that the instruction ran on the host is not.
        handle.write(f"FROM {BUILD_BASE_IMAGE}\nRUN printf '{marker}' > /tmp/host-side-build-marker\n")
    with open(os.path.join(infra, "compose.base.yml"), "w") as handle:
        handle.write(f"services:\n  {SERVICE_NAME}:\n    build:\n      context: ..\n      dockerfile: Dockerfile\n")
    with open(os.path.join(infra, "compose.dev.yml"), "w") as handle:
        handle.write("services: {}\n")
    return workspace


async def _issue_credential(worker_id: str, token: str, worker_type: WorkerType, workspace: str) -> None:
    """Create the worker's server-side records exactly as worker creation does.

    `_register_broker_worker` is the production call and is used unchanged. The
    two `worker:meta` fields are the ones `create_worker_with_capabilities` and
    `create_worker` write around it, and the order matters: the type is recorded
    before the credential exists, which is why an unrecorded type can be refused
    rather than tolerated.
    """
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.hset(f"worker:meta:{worker_id}", "worker_type", worker_type.value)
        await WorkerManager(redis)._register_broker_worker(worker_id, token, worker_type.value)
        await redis.hset(f"worker:meta:{worker_id}", "workspace_path", workspace)
    finally:
        await redis.aclose()


async def _revoke_credential(worker_id: str) -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await WorkerManager(redis)._unregister_broker_worker(worker_id)
        await redis.delete(f"worker:meta:{worker_id}", f"worker:broker:{worker_id}")
    finally:
        await redis.aclose()


class Worker:
    """A registered worker plus the two ways its token can ask for a build."""

    def __init__(self, worker_id: str, token: str, marker: str, workspace: str):
        self.worker_id = worker_id
        self.token = token
        self.marker = marker
        self.workspace = workspace

    def _post(self, url: str) -> httpx.Response:
        with httpx.Client(timeout=BUILD_TIMEOUT_SECONDS) as client:
            return client.post(
                url,
                json={"args": ["build"], "timeout": BUILD_TIMEOUT_SECONDS - 60},
                headers={"X-Worker-Broker-Token": self.token},
            )

    def build_through_broker(self) -> httpx.Response:
        return self._post(f"{BROKER_URL}/v1/workers/{self.worker_id}/infra/compose")

    def build_against_worker_manager(self) -> httpx.Response:
        """The same request with the broker skipped — a shell can do exactly this."""
        return self._post(f"{WORKER_MANAGER_URL}/api/worker/{self.worker_id}/infra/compose")


@pytest.fixture(scope="module")
def daemon():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # noqa: BLE001 — a boundary test without Docker proves nothing
        pytest.skip(f"no Docker daemon available: {exc}")
    return client


@pytest.fixture
def worker(daemon, request):
    """A registered worker of the requested type, with a project it could build."""
    worker_type = request.param
    run_id = secrets.token_hex(4)
    worker_id = f"cpb-{worker_type.value}-{run_id}"
    marker = f"host-side-build-{run_id}"
    workspace = _write_buildable_project(worker_id, marker)
    # Compose routes the project's default network to the worker's dev network,
    # which exists for a real worker. Creating it here keeps the developer
    # control honest: the build must fail for policy reasons or not at all.
    network = daemon.networks.create(f"dev_proj_{worker_id}", driver="bridge")
    # The credential the container would be started with, held here the way the
    # agent holds it: readable, and no more powerful for being read.
    token = secrets.token_urlsafe(32)
    asyncio.run(_issue_credential(worker_id, token, worker_type, workspace))
    try:
        yield Worker(worker_id, token, marker, workspace)
    finally:
        asyncio.run(_revoke_credential(worker_id))
        try:
            daemon.images.remove(_build_image_tag(worker_id), force=True)
        except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
            pass
        network.remove()


def _image_exists(daemon, worker_id: str) -> bool:
    try:
        daemon.images.get(_build_image_tag(worker_id))
    except docker.errors.ImageNotFound:
        return False
    return True


@pytest.mark.parametrize("worker", [WorkerType.DEVELOPER], indirect=True)
def test_a_developer_worker_really_can_build_on_the_management_host(daemon, worker):
    """The positive control. Without it, the refusal below could be an outage."""
    response = worker.build_through_broker()

    assert response.status_code == 200, response.text
    assert response.json()["exit_code"] == 0, response.json()
    assert _image_exists(daemon, worker.worker_id), "the developer build produced no image"

    # The image is not enough: run it and read what the `RUN` instruction wrote.
    output = daemon.containers.run(
        _build_image_tag(worker.worker_id),
        entrypoint=["cat", "/tmp/host-side-build-marker"],
        remove=True,
    )
    assert worker.marker.encode() in output, output


@pytest.mark.parametrize("worker", [WorkerType.QA], indirect=True)
def test_a_qa_worker_cannot_build_anything_with_its_own_token(daemon, worker):
    """Identical workspace, identical request, one difference: the recorded type."""
    through_broker = worker.build_through_broker()
    assert through_broker.status_code == 403, through_broker.text
    assert through_broker.json()["detail"] == "a qa worker may not call infra.compose"

    # The broker is not the only door: the token itself is readable by the agent,
    # so worker-manager is asked directly too.
    direct = worker.build_against_worker_manager()
    assert direct.status_code == 403, direct.text
    assert direct.json()["detail"] == "a qa worker may not call infra.compose"

    assert not _image_exists(daemon, worker.worker_id), "a QA worker caused a build on the management host"
    assert not os.path.exists(os.path.join(WORKSPACES, ".compose-plans", worker.worker_id)), (
        "a QA worker got as far as a compiled Compose plan"
    )


@pytest.mark.parametrize("worker", [WorkerType.QA], indirect=True)
def test_the_qa_worker_still_runs_its_own_turn(worker):
    """The refusal is scoped to control-plane authority, not to doing QA."""
    with httpx.Client(timeout=30) as client:
        headers = {"X-Worker-Broker-Token": worker.token}
        base = f"{BROKER_URL}/v1/workers/{worker.worker_id}"

        lease = client.post(f"{base}/input/lease", headers=headers)
        assert lease.status_code == 204, lease.text

        status = client.post(f"{base}/status", json={"values": {"status": "running"}}, headers=headers)
        assert status.status_code == 200, status.text

        session = client.put(f"{base}/session", json={"session_id": "qa-session"}, headers=headers)
        assert session.status_code == 200, session.text
        assert client.get(f"{base}/session", headers=headers).json()["session_id"] == "qa-session"

        result = client.post(
            f"{base}/output",
            json={"lease_id": "0-0", "result": {"status": "failed", "error": "no deployment under test"}},
            headers=headers,
        )
        assert result.status_code == 200, result.text


@pytest.mark.parametrize("worker", [WorkerType.QA], indirect=True)
def test_a_qa_token_cannot_borrow_another_workers_identity(worker):
    """Denied by the type on the record, not by which worker id was typed in."""
    with httpx.Client(timeout=30) as client:
        stolen = client.post(
            f"{BROKER_URL}/v1/workers/some-developer-worker/infra/compose",
            json={"args": ["build"]},
            headers={"X-Worker-Broker-Token": worker.token},
        )
    assert stolen.status_code == 403, stolen.text
    assert hashlib.sha256(worker.token.encode()).hexdigest() not in stolen.text
