"""What happens to a running worker when the new control plane is deployed.

Worker containers and their Redis records are not Compose services: replacing
worker-broker and worker-manager leaves live developer workers running, holding
credentials issued by the previous release. Those records carry no
`worker_type`, and every control-plane route now decides from one — so without
the startup migration, deploying this branch takes the lease, status, session,
result and Compose routes away from a developer worker in the middle of real
product work.

Nothing here is stubbed. The pre-cutover record is written into the real Redis
exactly as the previous release wrote it, the rollout is the two real service
containers being restarted, and the questions are asked over HTTP against the
real broker and the real worker-manager.

The QA credential is carried through the same restart as the control: the
migration must rescue the old developer worker without softening anything for
the executor this branch introduces.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import socket
import time

import docker
import httpx
import pytest
from redis.asyncio import Redis

from shared.contracts.vocab import WorkerType
from src.manager import WorkerManager

BROKER_URL = os.environ["WORKER_BROKER_URL"].rstrip("/")
WORKER_MANAGER_URL = os.environ["WORKER_MANAGER_URL"].rstrip("/")
REDIS_URL = os.environ["REDIS_URL"]

RESTART_TIMEOUT_SECONDS = 90
ROLLED_OUT_SERVICES = ("worker-broker", "worker-manager")


async def _write_pre_cutover_records(worker_id: str, token: str) -> None:
    """The records the previous release left behind, field for field.

    Taken from `services/worker-broker/src/main.py` and
    `services/worker-manager/src/manager.py` at `fdeaa770`, the base this branch
    was reviewed against: a credential with no type, a worker record with no
    type, and the consumer group a worker mid-turn already has.
    """
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.hset(
            f"worker:broker:{worker_id}",
            mapping={
                "token_digest": hashlib.sha256(token.encode()).hexdigest(),
                "input_stream": f"worker:{worker_id}:input",
                "output_stream": f"worker:{worker_id}:output",
                "consumer_group": "worker_group",
                "session_ttl_seconds": "3600",
            },
        )
        await redis.hset(f"worker:meta:{worker_id}", "workspace_path", f"/data/workspaces/{worker_id}")
        await redis.xgroup_create(f"worker:{worker_id}:input", "worker_group", id="0", mkstream=True)
    finally:
        await redis.aclose()


async def _register_qa_worker(worker_id: str, token: str) -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.hset(f"worker:meta:{worker_id}", "worker_type", WorkerType.QA.value)
        await WorkerManager(redis)._register_broker_worker(worker_id, token, WorkerType.QA.value)
    finally:
        await redis.aclose()


async def _forget(*worker_ids: str) -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        for worker_id in worker_ids:
            await WorkerManager(redis)._unregister_broker_worker(worker_id)
            await redis.delete(
                f"worker:broker:{worker_id}",
                f"worker:meta:{worker_id}",
                f"worker:status:{worker_id}",
                f"worker:session:{worker_id}",
                f"worker:{worker_id}:input",
                f"worker:{worker_id}:output",
            )
    finally:
        await redis.aclose()


async def _queue_input(worker_id: str, payload: str) -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await redis.xadd(f"worker:{worker_id}:input", {"data": payload})
    finally:
        await redis.aclose()


def _post(url: str, token: str, json: dict) -> httpx.Response:
    with httpx.Client(timeout=30) as client:
        return client.post(url, json=json, headers={"X-Worker-Broker-Token": token})


def _own_compose_project(daemon) -> str:
    """The Compose project this test runner belongs to.

    The management host runs other stacks with services of the same names, so
    the restart has to be scoped to this one or it would bounce a developer's
    live installation.
    """
    container = daemon.containers.get(socket.gethostname())
    project = container.labels.get("com.docker.compose.project")
    if not project:
        pytest.skip("this runner is not part of a Compose project, so a rollout cannot be scoped")
    return project


def _roll_out(daemon, project: str) -> None:
    """Restart the control plane the way a deploy does, and wait for it back."""
    for service in ROLLED_OUT_SERVICES:
        containers = daemon.containers.list(
            all=True,
            filters={"label": [f"com.docker.compose.project={project}", f"com.docker.compose.service={service}"]},
        )
        assert containers, f"no {service} container in project {project}"
        for container in containers:
            # Docker's restart endpoint can hold its Unix-socket response open
            # after the process has already come back. A deploy only requires a
            # replacement process; an explicit kill/start provides that same
            # transition without coupling the test runner to that response.
            container.kill()
            container.start()

    deadline = time.monotonic() + RESTART_TIMEOUT_SECONDS
    for url in (f"{BROKER_URL}/docs", f"{WORKER_MANAGER_URL}/health"):
        while True:
            try:
                with httpx.Client(timeout=5) as client:
                    if client.get(url).status_code == 200:
                        break
            except Exception:  # noqa: BLE001 — a service still starting is not yet a failure
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"{url} did not come back within {RESTART_TIMEOUT_SECONDS}s")
            time.sleep(1)


@pytest.fixture(scope="module")
def daemon():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # noqa: BLE001 — a rollout test without Docker proves nothing
        pytest.skip(f"no Docker daemon available: {exc}")
    return client


@pytest.fixture
def rollout(daemon):
    """A live pre-cutover developer worker and a QA worker, across a restart."""
    run_id = secrets.token_hex(4)
    legacy_id, legacy_token = f"legacy-dev-{run_id}", secrets.token_urlsafe(32)
    qa_id, qa_token = f"rollout-qa-{run_id}", secrets.token_urlsafe(32)
    asyncio.run(_write_pre_cutover_records(legacy_id, legacy_token))
    asyncio.run(_register_qa_worker(qa_id, qa_token))
    try:
        yield legacy_id, legacy_token, qa_id, qa_token, _own_compose_project(daemon)
    finally:
        asyncio.run(_forget(legacy_id, qa_id))


def test_a_worker_from_before_the_cutover_survives_the_control_plane_rollout(daemon, rollout):
    legacy_id, legacy_token, qa_id, qa_token, project = rollout

    # Before the rollout the strict policy refuses the old record — this is the
    # outage the migration exists to prevent, observed rather than assumed.
    refused = _post(f"{BROKER_URL}/v1/workers/{legacy_id}/input/lease", legacy_token, {})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "worker type is not recorded for this worker"

    _roll_out(daemon, project)

    # The whole turn protocol, with the credential the running wrapper holds.
    asyncio.run(_queue_input(legacy_id, '{"task_id": "rollout-1", "prompt": "keep going"}'))
    lease = _post(f"{BROKER_URL}/v1/workers/{legacy_id}/input/lease", legacy_token, {})
    assert lease.status_code == 200, lease.text
    assert lease.json()["data"]["task_id"] == "rollout-1"

    status = _post(f"{BROKER_URL}/v1/workers/{legacy_id}/status", legacy_token, {"values": {"status": "running"}})
    assert status.status_code == 200, status.text

    with httpx.Client(timeout=30) as client:
        headers = {"X-Worker-Broker-Token": legacy_token}
        session_write = client.put(
            f"{BROKER_URL}/v1/workers/{legacy_id}/session", json={"session_id": "resumed"}, headers=headers
        )
        assert session_write.status_code == 200, session_write.text
        session_read = client.get(f"{BROKER_URL}/v1/workers/{legacy_id}/session", headers=headers)
        assert session_read.json()["session_id"] == "resumed", session_read.text

    submitted = _post(
        f"{BROKER_URL}/v1/workers/{legacy_id}/output",
        legacy_token,
        {"lease_id": lease.json()["lease_id"], "result": {"status": "failed", "error": "finished after rollout"}},
    )
    assert submitted.status_code == 200, submitted.text

    # And the developer route this side of the hop. The command is one the
    # runner refuses, so authorization is what is being read: 400 means the
    # request was authorized and then rejected on its merits, 403 would mean
    # the worker lost the route.
    for url in (
        f"{BROKER_URL}/v1/workers/{legacy_id}/infra/compose",
        f"{WORKER_MANAGER_URL}/api/worker/{legacy_id}/infra/compose",
    ):
        compose = _post(url, legacy_token, {"args": ["exec", "db", "bash"]})
        assert compose.status_code == 400, f"{url} -> {compose.status_code} {compose.text}"

    # The rescue is scoped: the QA executor's recorded type survived the restart
    # and still buys it nothing on the management host.
    for url in (
        f"{BROKER_URL}/v1/workers/{qa_id}/infra/compose",
        f"{WORKER_MANAGER_URL}/api/worker/{qa_id}/infra/compose",
    ):
        denied = _post(url, qa_token, {"args": ["ps"]})
        assert denied.status_code == 403, f"{url} -> {denied.status_code} {denied.text}"
        assert denied.json()["detail"] == "a qa worker may not call infra.compose"


def test_a_typeless_record_created_after_the_rollout_is_still_refused():
    """The migration is a cutover, not a standing fallback.

    It runs at startup over what was there. A record written afterwards without
    a type — which registration cannot produce — is refused every route, which
    is the point: the request path never consults the migration.
    """
    stray_id, stray_token = f"stray-{secrets.token_hex(4)}", secrets.token_urlsafe(32)
    asyncio.run(_write_pre_cutover_records(stray_id, stray_token))
    try:
        refused = _post(f"{BROKER_URL}/v1/workers/{stray_id}/input/lease", stray_token, {})
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == "worker type is not recorded for this worker"
    finally:
        asyncio.run(_forget(stray_id))
