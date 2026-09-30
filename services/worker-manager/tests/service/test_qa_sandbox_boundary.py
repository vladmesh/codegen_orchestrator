"""The QA sandbox as worker-manager really builds it, against a real Docker daemon.

The executor here is not assembled by the test. It is created by
`WorkerManager.create_worker_with_capabilities` — the path a `qa` create command
takes — with a deploy target and Telegram entries, and removed by
`delete_worker`. What the test then reads is what Docker says about that
container, and what happens to packets sent from inside it:

* its environment holds no platform secret, although this process holds all of
  them while it builds the container;
* it has no bind mount of the orchestrator `.env`, an SSH key directory or the
  GitHub App key, and exactly one secret mount: the CLI's own auth directory;
* it is attached to the internal network alone;
* through its proxy it reaches the run's deploy target (the network carries a
  write as well as a read — forbidding direct application writes is the QA
  runtime's policy and evidence guard, not this layer — and a plain-`http://`
  target is reached with `curl --proxytunnel`), an address inside a Telegram network and its model
  backend, and nothing else — not a platform service, not a neighbour address,
  not the target's port 22;
* without the proxy it reaches nothing off its network at all.

Three things are stood in by local listeners on the outside network: the model
backend, the deploy target (named `target.qa-sandbox.test`, which the
deploy-target check accepts as a public name), and "Telegram" (one listener
whose address is the only Telegram network of the run). The image is built on
the stack's own worker-manager image — it has python3 and curl — and differs
only in running `sleep` as `worker` (uid 1000, as in the real worker images).
In the service compose this runner and the Docker daemon see different
filesystems, so the bind sources are prepared on the daemon's side by a short
helper container, as `_prepare_remote_daemon_mounts` does for a remote daemon.
The image build and the Telethon tooling are proven elsewhere; nothing here
depends on them.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import secrets
import shutil
import tempfile

import docker
import pytest
from redis.asyncio import Redis

from shared.contracts.queues.worker import WorkerOwnership
from shared.contracts.vocab import AgentType
from src import qa_egress, workspace as workspace_mod
from src.config import settings
from src.container_config import CLAUDE_CONFIG_DIR
from src.manager import QA_WORKER_TYPE, WorkerManager

TEST_IMAGE = os.environ.get("QA_EGRESS_TEST_IMAGE", "codegen-orchestrator/worker-manager:test")
REDIS_URL = os.environ.get("REDIS_URL", "")

TARGET_HOST = "target.qa-sandbox.test"
TARGET_PORT = 8080
BACKEND_PORT = 8443
TELEGRAM_PORT = 8443
CAPABILITY_PORT = 9000
PLATFORM_PORT = 8000

SANDBOX_DOCKERFILE = f"""\
FROM {TEST_IMAGE}
USER root
RUN (getent group 1000 || groupadd -g 1000 worker) \\
    && useradd -o -u 1000 -g 1000 -M -d /home/worker worker \\
    && mkdir -p /home/worker && chown 1000:1000 /home/worker
USER worker
WORKDIR /workspace
ENTRYPOINT ["sleep", "infinity"]
"""

# Answers every request and keeps a ledger of what it was sent, so a request
# that arrived is a fact of the listener and not of the client's exit code.
RECORDER = """
import http.server, json, sys
NAME = sys.argv[1]
PORT = int(sys.argv[2])
RECORDED = []
class Handler(http.server.BaseHTTPRequestHandler):
    def _answer(self, status, body):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def do_GET(self):
        if self.path == "/__recorded":
            self._answer(200, json.dumps(RECORDED))
            return
        RECORDED.append("GET " + self.path)
        self._answer(200, NAME + "-answered")
    def _write(self):
        RECORDED.append(self.command + " " + self.path)
        self._answer(201, NAME + "-wrote")
    do_POST = _write
    do_PUT = _write
    do_DELETE = _write
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
"""

# What a Telegram data centre is to this test: something that answers bytes.
ECHO = """
import socket, sys, threading
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("0.0.0.0", int(sys.argv[1])))
server.listen()
def serve(conn):
    with conn:
        data = conn.recv(4096)
        conn.sendall(b"mtproto-standin:" + data)
while True:
    conn, _ = server.accept()
    threading.Thread(target=serve, args=(conn,), daemon=True).start()
"""

# CONNECT by hand, then one exchange through the tunnel. Exit 0 only when the
# proxy opened the tunnel; the answer is printed either way.
TUNNEL = """
import socket, sys
proxy, target, payload = sys.argv[1], sys.argv[2], sys.argv[3].encode().decode("unicode_escape")
host, port = proxy.rsplit(":", 1)
s = socket.create_connection((host, int(port)), 10)
s.sendall(("CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n" % (target, target)).encode())
answer = s.recv(4096)
sys.stdout.write(answer.decode("latin-1", "replace"))
if not answer.startswith(b"HTTP/1.1 200"):
    raise SystemExit(1)
s.sendall(payload.encode("latin-1"))
s.settimeout(10)
while True:
    try:
        chunk = s.recv(4096)
    except socket.timeout:
        break
    if not chunk:
        break
    sys.stdout.write(chunk.decode("latin-1", "replace"))
"""

# A direct TCP connect, as a process that ignores every proxy variable would try.
DIRECT = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), 5)
except OSError as exc:
    print("unreachable", type(exc).__name__)
    raise SystemExit(1)
print("connected")
"""

# Everything the management host holds that a sandbox must never see. It is put
# into this process's environment while the executor is built, so its absence
# from the container is the creation path's doing, not an empty environment's.
PLATFORM_SECRETS = {
    "SECRETS_ENCRYPTION_KEY": "platform-encryption-key",
    "TELETHON_API_ID": "12345",
    "TELETHON_API_HASH": "platform-telethon-hash",
    "TELETHON_SESSION": "platform-telethon-session",
    "DATABASE_URL": "postgresql://platform:secret@postgres/platform",
    "REDIS_URL": REDIS_URL or "redis://redis:6379/0",
    "GITHUB_TOKEN": "platform-github-token",
    "GITHUB_APP_PRIVATE_KEY_PATH": "/run/secrets/github-app.pem",
    "REGISTRY_PASSWORD": "platform-registry-password",
}
FORBIDDEN_ENV = ("SECRETS_ENCRYPTION_KEY", "DATABASE_URL", "REDIS_URL", "REGISTRY_PASSWORD")


def _wait_for_port(container, host, port, attempts=40):
    probe = (
        "import socket, sys, time\n"
        f"for _ in range({attempts}):\n"
        "    try:\n"
        f"        socket.create_connection(({host!r}, {port}), 1); sys.exit(0)\n"
        "    except OSError:\n"
        "        time.sleep(0.25)\n"
        "sys.exit(1)"
    )
    exit_code, output = container.exec_run(["python3", "-c", probe])
    assert exit_code == 0, f"{host}:{port} never answered: {output!r}"


def _ip(container, network) -> str:
    container.reload()
    return container.attrs["NetworkSettings"]["Networks"][network.name]["IPAddress"]


@pytest.fixture(scope="module")
def daemon():
    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:  # noqa: BLE001 — a boundary test without Docker proves nothing
        pytest.skip(f"no Docker daemon available: {exc}")
    try:
        client.images.get(TEST_IMAGE)
    except docker.errors.ImageNotFound:
        pytest.skip(f"{TEST_IMAGE} is not built here")
    if not REDIS_URL:
        pytest.skip("REDIS_URL is not set; the creation path records its worker in Redis")
    return client


@pytest.fixture(scope="module")
def sandbox(daemon):  # noqa: PLR0915 — one run's whole world, set up and torn down in order
    """A QA run's world, and an executor worker-manager built into it."""
    run_id = f"qasandbox{secrets.token_hex(4)}"
    worker_id = f"qa-{run_id}"
    image = f"qa-sandbox-test:{run_id}"
    created: list = []
    networks: list = []
    scratch = tempfile.mkdtemp(prefix=f"{run_id}-")
    claude_dir = os.path.join(scratch, "claude-profile")
    os.makedirs(claude_dir)
    with open(os.path.join(claude_dir, ".credentials.json"), "w") as handle:
        json.dump(
            {
                "claudeAiOauth": {
                    "accessToken": "test-access",
                    "refreshToken": "test-refresh",
                    "expiresAt": 4102444800000,
                }
            },
            handle,
        )
    patch = pytest.MonkeyPatch()
    manager_created = False
    try:
        daemon.images.build(fileobj=io.BytesIO(SANDBOX_DOCKERFILE.encode()), tag=image, rm=True)
        outside = daemon.networks.create(f"{run_id}-outside", driver="bridge")
        networks.append(outside)
        inside = daemon.networks.create(f"{run_id}-run", driver="bridge", internal=True)
        networks.append(inside)

        def start(name, script, args, network, alias):
            """A listener on `network`, addressable there by `alias` and nothing shorter."""
            container = daemon.containers.create(
                TEST_IMAGE,
                entrypoint=["python3", "-c", script, *args],
                name=f"{run_id}-{name}",
                network=network.name,
            )
            created.append(container)
            network.disconnect(container)
            network.connect(container, aliases=[alias])
            container.start()
            return container

        target = start("target", RECORDER, ["target", str(TARGET_PORT)], outside, TARGET_HOST)
        # A platform service on the proxy's outside leg, as `api` is in production.
        platform = start("platform", RECORDER, ["platform", str(PLATFORM_PORT)], outside, "api")
        backend = start("backend", RECORDER, ["backend", str(BACKEND_PORT)], outside, "backend")
        telegram = start("telegram", ECHO, [str(TELEGRAM_PORT)], outside, "telegram")
        capability = start(
            "capability", RECORDER, ["capability", str(CAPABILITY_PORT)], inside, "capability"
        )
        control = daemon.containers.run(
            TEST_IMAGE,
            entrypoint=["sleep", "infinity"],
            name=f"{run_id}-control",
            network=outside.name,
            detach=True,
        )
        created.append(control)
        for host, port in (
            (TARGET_HOST, TARGET_PORT),
            ("api", PLATFORM_PORT),
            ("backend", BACKEND_PORT),
            (_ip(telegram, outside), TELEGRAM_PORT),
        ):
            _wait_for_port(control, host, port)
        telegram_ip = _ip(telegram, outside)

        # Held for the fixture's whole life, so `delete_worker` below sees the
        # same world `create_worker_with_capabilities` built the executor in.
        for name, value in PLATFORM_SECRETS.items():
            patch.setenv(name, value)
        patch.setattr(settings, "DOCKER_NETWORK", "")
        patch.setattr(settings, "QA_EGRESS_NETWORK", inside.name)
        patch.setattr(settings, "WORKER_NETWORK", outside.name)
        patch.setattr(settings, "QA_CLAUDE_BACKEND_HOSTS", f"backend:{BACKEND_PORT}")
        patch.setattr(settings, "SCAFFOLDED_WORKSPACE_PATH", os.path.join(scratch, "ws"))
        patch.setattr(
            settings, "WORKER_TRANSCRIPT_STORAGE_PATH", os.path.join(scratch, "transcripts")
        )
        patch.setattr(settings, "HOST_CLAUDE_VALIDATION_PATH", None)
        # The one Telegram network of this run is the stand-in's address.
        patch.setattr(qa_egress, "TELEGRAM_MTPROTO_NETWORKS", (f"{telegram_ip}/32",))
        patch.setattr(qa_egress, "TELEGRAM_MTPROTO_PORTS", (TELEGRAM_PORT,))
        os.makedirs(settings.SCAFFOLDED_WORKSPACE_PATH)
        # The bind sources, owned by the worker on the daemon's filesystem.
        daemon.containers.run(
            image,
            entrypoint=[
                "sh",
                "-c",
                f"mkdir -p /scratch/ws/{workspace_mod.QA_WORKSPACE_PREFIX}{worker_id} "
                "/scratch/transcripts && chown -R 1000:1000 /scratch",
            ],
            user="root",
            volumes={scratch: {"bind": "/scratch", "mode": "rw"}},
            network_mode="none",
            remove=True,
        )

        async def create() -> None:
            redis = Redis.from_url(REDIS_URL, decode_responses=True)
            manager = WorkerManager(redis)

            async def prebuilt(**_kwargs):
                return image

            # The capability image build is the one step replaced: its
            # Dockerfile is unit-tested, and the tooling it installs is not
            # what this boundary depends on.
            manager.ensure_or_build_image = prebuilt
            try:
                await manager.create_worker_with_capabilities(
                    worker_id=worker_id,
                    capabilities=["qa_sandbox"],
                    base_image="unused-prebuilt",
                    ownership=WorkerOwnership(
                        story_id=f"story-{run_id}",
                        project_id=f"proj-{run_id}",
                        run_id=f"run-{run_id}",
                        attempt_id=f"attempt-{run_id}",
                    ),
                    agent_type=AgentType.CLAUDE,
                    instructions="# QA executor",
                    task_content="test the product",
                    auth_mode="host_session",
                    host_claude_dir=claude_dir,
                    env_vars={
                        "QA_CAPABILITY_URL": f"http://capability:{CAPABILITY_PORT}/qa/call",
                        "QA_CAPABILITY_TOKEN": "run-token",
                    },
                    worker_type=QA_WORKER_TYPE,
                    qa_target_url=f"http://{TARGET_HOST}:{TARGET_PORT}",
                )
            finally:
                await redis.aclose()

        manager_created = True
        asyncio.run(create())

        executor = daemon.containers.get(f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}")
        yield {
            "daemon": daemon,
            "executor": executor,
            "inside": inside,
            "outside": outside,
            "claude_dir": claude_dir,
            "proxy": f"{qa_egress.proxy_container_name(worker_id)}:3128",
            "target": target,
            "target_ip": _ip(target, outside),
            "platform": platform,
            "platform_ip": _ip(platform, outside),
            "backend": backend,
            "telegram_ip": telegram_ip,
            "capability": capability,
            "control": control,
        }
    finally:
        if manager_created:

            async def delete() -> None:
                redis = Redis.from_url(REDIS_URL, decode_responses=True)
                try:
                    await WorkerManager(redis).delete_worker(worker_id, reason="completed")
                finally:
                    await redis.aclose()

            try:
                asyncio.run(delete())
            except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
                pass
        for name in (
            f"{settings.WORKER_IMAGE_PREFIX}-{worker_id}",
            qa_egress.proxy_container_name(worker_id),
        ):
            try:
                daemon.containers.get(name).remove(force=True)
            except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
                pass
        for container in created:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
                pass
        for network in networks:
            try:
                network.remove()
            except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
                pass
        try:
            daemon.images.remove(image, force=True)
        except Exception:  # noqa: BLE001, S110 — teardown of a test fixture
            pass
        patch.undo()
        shutil.rmtree(scratch, ignore_errors=True)


def _in_executor(sandbox, command, *, unset_proxy=False):
    environment = None
    if unset_proxy:
        environment = dict.fromkeys(
            ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"],
            "",
        )
    return sandbox["executor"].exec_run(command, environment=environment)


def _ledger(sandbox, host, port):
    answer = sandbox["control"].exec_run(
        ["curl", "-sS", "-m", "10", f"http://{host}:{port}/__recorded"]
    )
    assert answer.exit_code == 0, answer.output
    return json.loads(answer.output.decode().strip().splitlines()[-1])


def test_the_sandbox_env_holds_no_platform_secret(sandbox):
    attrs = sandbox["daemon"].api.inspect_container(sandbox["executor"].id)
    env = dict(item.split("=", 1) for item in attrs["Config"]["Env"])

    for name in FORBIDDEN_ENV:
        assert name not in env, f"{name} reached the sandbox"
    for name in env:
        assert not name.startswith("TELETHON_"), f"{name} reached the sandbox"
        assert not name.startswith("GITHUB_"), f"{name} reached the sandbox"
    assert "GH_TOKEN" not in env
    for value in PLATFORM_SECRETS.values():
        assert value not in env.values()
    # What it was given instead: its endpoint and the proxy door.
    assert env["QA_CAPABILITY_TOKEN"] == "run-token"  # noqa: S105 — the fixture's run token
    assert env["HTTPS_PROXY"] == f"http://{sandbox['proxy']}"


def test_the_only_secret_mount_is_the_clis_own_auth_directory(sandbox):
    attrs = sandbox["daemon"].api.inspect_container(sandbox["executor"].id)
    mounts = {mount["Destination"]: mount for mount in attrs["Mounts"]}

    assert set(mounts) == {
        "/workspace",
        CLAUDE_CONFIG_DIR,
        "/home/worker/.cache/uv",
    }
    # The one secret mount, named: the Claude CLI's session directory.
    assert mounts[CLAUDE_CONFIG_DIR]["Source"] == sandbox["claude_dir"]
    for mount in attrs["Mounts"]:
        source = mount.get("Source", "")
        assert not source.endswith(".env"), source
        assert ".ssh" not in source, source
        assert not source.endswith(".pem"), source


def test_the_sandbox_is_on_the_internal_network_alone(sandbox):
    attrs = sandbox["daemon"].api.inspect_container(sandbox["executor"].id)

    assert set(attrs["NetworkSettings"]["Networks"]) == {sandbox["inside"].name}
    assert sandbox["daemon"].api.inspect_network(sandbox["inside"].id)["Internal"] is True
    # The runtime's own services stay reachable on that network.
    _wait_for_port(sandbox["executor"], "capability", CAPABILITY_PORT)


def test_the_deploy_target_is_tunnelled_writes_included(sandbox):
    # Plain http:// through the CONNECT-only proxy: curl's --proxytunnel.
    write = _in_executor(
        sandbox,
        [
            "curl",
            "-sS",
            "-m",
            "10",
            "--proxytunnel",
            "-x",
            f"http://{sandbox['proxy']}",
            "-X",
            "POST",
            f"http://{TARGET_HOST}:{TARGET_PORT}/orders",
            "-d",
            "{}",
        ],
    )
    assert write.exit_code == 0, write.output
    assert b"target-wrote" in write.output

    raw = _in_executor(
        sandbox,
        [
            "python3",
            "-c",
            TUNNEL,
            sandbox["proxy"],
            f"{TARGET_HOST}:{TARGET_PORT}",
            f"GET /status HTTP/1.1\\r\\nHost: {TARGET_HOST}\\r\\nConnection: close\\r\\n\\r\\n",
        ],
    )
    assert raw.exit_code == 0, raw.output
    assert b"target-answered" in raw.output

    assert _ledger(sandbox, TARGET_HOST, TARGET_PORT) == ["POST /orders", "GET /status"]


def test_an_address_inside_a_telegram_network_is_tunnelled(sandbox):
    answer = _in_executor(
        sandbox,
        [
            "python3",
            "-c",
            TUNNEL,
            sandbox["proxy"],
            f"{sandbox['telegram_ip']}:{TELEGRAM_PORT}",
            "hello",
        ],
    )

    assert answer.exit_code == 0, answer.output
    assert b"mtproto-standin:hello" in answer.output


def test_the_model_backend_is_tunnelled(sandbox):
    answer = _in_executor(
        sandbox,
        [
            "python3",
            "-c",
            TUNNEL,
            sandbox["proxy"],
            f"backend:{BACKEND_PORT}",
            "GET / HTTP/1.1\\r\\nHost: backend\\r\\nConnection: close\\r\\n\\r\\n",
        ],
    )

    assert answer.exit_code == 0, answer.output
    assert b"backend-answered" in answer.output


@pytest.mark.parametrize(
    "destination",
    [
        "api:8000",
        "platform-ip",
        f"{TARGET_HOST}:22",
        "target-ip",
        f"telegram-ip:{TELEGRAM_PORT + 1}",
        "example.com:443",
    ],
)
def test_every_other_destination_is_refused(sandbox, destination):
    destination = (
        destination.replace("platform-ip", f"{sandbox['platform_ip']}:{PLATFORM_PORT}")
        .replace("target-ip", f"{sandbox['target_ip']}:{TARGET_PORT}")
        .replace("telegram-ip", sandbox["telegram_ip"])
    )

    answer = _in_executor(
        sandbox,
        [
            "python3",
            "-c",
            TUNNEL,
            sandbox["proxy"],
            destination,
            "POST /orders HTTP/1.1\\r\\nContent-Length: 0\\r\\n\\r\\n",
        ],
    )

    assert answer.exit_code != 0
    assert b"403" in answer.output, answer.output


def test_the_proxy_stays_connect_only(sandbox):
    forwarded = _in_executor(
        sandbox,
        [
            "curl",
            "-sS",
            "-m",
            "8",
            "-x",
            f"http://{sandbox['proxy']}",
            f"http://{TARGET_HOST}:{TARGET_PORT}/orders",
        ],
    )

    assert b"CONNECT only" in forwarded.output, forwarded.output


def test_nothing_off_the_network_is_reachable_directly(sandbox):
    for host, port in (
        (sandbox["target_ip"], TARGET_PORT),
        (TARGET_HOST, TARGET_PORT),
        (sandbox["telegram_ip"], TELEGRAM_PORT),
        (sandbox["platform_ip"], PLATFORM_PORT),
        ("backend", BACKEND_PORT),
    ):
        attempt = _in_executor(
            sandbox, ["python3", "-c", DIRECT, host, str(port)], unset_proxy=True
        )
        assert attempt.exit_code != 0, f"{host}:{port} was reachable: {attempt.output!r}"


def test_the_platform_service_received_nothing(sandbox):
    """Run last in this module: every refusal above aimed at it, and it heard none."""
    assert _ledger(sandbox, "api", PLATFORM_PORT) == []
