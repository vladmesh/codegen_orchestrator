"""The QA executor's write boundary, against a real Docker daemon.

This is the test the guarantee rests on, so it substitutes nothing: a real
recording application, a real executor container built by the real policy in
`src.qa_egress` and `src.container_config`, and real `POST/PUT/PATCH/DELETE`
attempts made from inside that container with raw `curl` and with a Python HTTP
client. The application counts every request it receives, and the assertion is
that it counted zero writes from the executor.

A test that only shows failures could be passing because the whole network is
broken, so the same test carries its positive controls:

* the recording application really does record — a container that *is* allowed
  to reach it writes to it, and the write shows up in its ledger;
* the allowed directions really are open — the run's capability endpoint answers
  the executor, and a tunnel to the allowlisted backend carries a request and a
  response;
* the refusals are the policy's, not a dead network — the same proxy that
  carries the allowed tunnel answers `403` for the application's address.

The real model backend (`api.anthropic.com`) is not reachable from CI and is not
contacted here. It is stood in for by a container on the outside network that is
named in the run's allowlist, which exercises the identical code path in the
proxy: allowlist lookup, `CONNECT`, tunnel. What this test therefore proves
about the model direction is that an allowlisted host is opened and every other
host is not — not that Anthropic answers.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets

import docker
import pytest

from shared.contracts.vocab import AgentType
from src import qa_egress
from src.container_config import WorkerContainerConfig
from src.docker_ops import DockerClientWrapper

# Any image with python3 and curl. The service stack has this one built; the
# executor's real image is an agent base that is far more expensive to produce
# and carries nothing this boundary depends on.
TEST_IMAGE = os.environ.get("QA_EGRESS_TEST_IMAGE", "codegen-orchestrator/worker-manager:test")

APP_PORT = 8080
BACKEND_PORT = 8443
CAPABILITY_PORT = 9000

RECORDING_APP = (
    """
import http.server, json
RECORDED = []
class Handler(http.server.BaseHTTPRequestHandler):
    def _answer(self, status, body):
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def _record(self):
        RECORDED.append(self.command + " " + self.path)
        print("APP_REQUEST " + self.command + " " + self.path, flush=True)
    def do_GET(self):
        if self.path == "/__recorded":
            self._answer(200, json.dumps(RECORDED))
            return
        self._record()
        self._answer(200, '{"ok": true}')
    def _write(self):
        self._record()
        self._answer(201, '{"created": true}')
    do_POST = _write
    do_PUT = _write
    do_PATCH = _write
    do_DELETE = _write
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", %d), Handler).serve_forever()
"""
    % APP_PORT
)

CAPABILITY_ENDPOINT = (
    """
import http.server, json
class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.dumps({"tool": "http_get", "status": 200}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", %d), Handler).serve_forever()
"""
    % CAPABILITY_PORT
)

MODEL_BACKEND = (
    """
import http.server
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"model-backend-answered"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.ThreadingHTTPServer(("0.0.0.0", %d), Handler).serve_forever()
"""
    % BACKEND_PORT
)

# Speaks CONNECT by hand: `curl` will not tunnel to a plain-HTTP origin, and the
# point of the check is the tunnel, not TLS.
TUNNEL = """
import socket, sys
proxy, target = sys.argv[1], sys.argv[2]
host, port = proxy.split(":")
s = socket.create_connection((host, int(port)), 10)
s.sendall(("CONNECT %s HTTP/1.1\\r\\nHost: %s\\r\\n\\r\\n" % (target, target)).encode())
answer = s.recv(4096)
sys.stdout.write(answer.decode("latin-1", "replace"))
if not answer.startswith(b"HTTP/1.1 200"):
    raise SystemExit(1)
s.sendall(b"GET / HTTP/1.1\\r\\nHost: backend\\r\\nConnection: close\\r\\n\\r\\n")
while True:
    chunk = s.recv(4096)
    if not chunk:
        break
    sys.stdout.write(chunk.decode("latin-1", "replace"))
"""


def _wait_for_port(client, container_id, host, port, attempts=40):
    """Block until a helper container answers, so a race cannot look like a refusal."""
    probe = (
        f'python3 -c "import socket,time,sys\n'
        f"for _ in range({attempts}):\n"
        f"    try:\n"
        f"        socket.create_connection(('{host}', {port}), 1); sys.exit(0)\n"
        f"    except OSError:\n"
        f"        time.sleep(0.25)\n"
        f'sys.exit(1)"'
    )
    exit_code, output = client.containers.get(container_id).exec_run(probe)
    assert exit_code == 0, f"{host}:{port} never answered: {output!r}"


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
    return client


@pytest.fixture(scope="module")
def scenario(daemon):
    """One QA run's real network: the deployment outside it, the executor inside."""
    run_id = f"qaegress{secrets.token_hex(4)}"
    created: list = []
    networks: list = []
    try:
        outside = daemon.networks.create(f"{run_id}-outside", driver="bridge")
        networks.append(outside)
        # The executor's own network. `internal` is the whole boundary.
        inside = daemon.networks.create(f"{run_id}-run", driver="bridge", internal=True)
        networks.append(inside)

        def start(name, script, network, **kwargs):
            container = daemon.containers.run(
                TEST_IMAGE,
                entrypoint=["python3", "-c", script],
                name=f"{run_id}-{name}",
                hostname=name,
                network=network.name,
                detach=True,
                **kwargs,
            )
            created.append(container)
            return container

        app = start("app", RECORDING_APP, outside)
        backend = start("backend", MODEL_BACKEND, outside)
        capability = start("capability", CAPABILITY_ENDPOINT, inside)
        # A container that is allowed to reach the application, so the ledger
        # below is a ledger and not a broken server.
        control = daemon.containers.run(
            TEST_IMAGE,
            entrypoint=["sleep", "infinity"],
            name=f"{run_id}-control",
            network=outside.name,
            detach=True,
        )
        created.append(control)

        app.reload()
        app_ip = app.attrs["NetworkSettings"]["Networks"][outside.name]["IPAddress"]
        _wait_for_port(daemon, control.id, app_ip, APP_PORT)

        yield {
            "daemon": daemon,
            "run_id": run_id,
            "inside": inside,
            "outside": outside,
            "app": app,
            "app_ip": app_ip,
            "backend": backend,
            "capability": capability,
            "control": control,
            "created": created,
        }
    finally:
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


def test_the_qa_executor_cannot_write_to_the_application(scenario):
    # Driven synchronously on purpose: the service runner has no asyncio mode
    # configured, and the only async parts here are two policy calls.
    daemon = scenario["daemon"]
    run_id = scenario["run_id"]
    app_ip = scenario["app_ip"]
    inside = scenario["inside"]
    worker_id = f"{run_id}-exec"
    wrapper = DockerClientWrapper()

    # --- the policy, from the code that runs in production -------------------
    egress = asyncio.run(
        qa_egress.establish(
            wrapper,
            worker_id=worker_id,
            agent_type=AgentType.CLAUDE,
            image=TEST_IMAGE,
            network=inside.name,
            internet_network=scenario["outside"].name,
            # The real model backend is unreachable from CI; an allowlisted host
            # on the outside network exercises the identical path through the proxy.
            configured_backends=f"backend:{BACKEND_PORT}",
            direct=("capability",),
        )
    )
    executor = None
    try:
        config = WorkerContainerConfig(
            worker_id=worker_id,
            worker_type="qa",
            agent_type=AgentType.CLAUDE,
            capabilities=[],
        )
        run_kwargs = config.to_docker_run_kwargs(network_name=inside.name)
        run_kwargs.update(
            {
                "image": TEST_IMAGE,
                "entrypoint": ["sleep", "infinity"],
                "environment": {
                    "QA_CAPABILITY_URL": f"http://capability:{CAPABILITY_PORT}/qa/call",
                    "QA_CAPABILITY_TOKEN": "run-token",
                    **egress.env_vars,
                },
            }
        )
        run_kwargs["name"] = f"{run_id}-exec"
        executor = daemon.containers.run(**run_kwargs)

        # The runtime's own fail-closed check, on the container that exists.
        qa_egress.verify_isolation(daemon.api.inspect_container(executor.id), inside.name)

        def in_executor(command, *, unset_proxy=False):
            """Run something in the executor, optionally as a hostile agent would.

            `unset_proxy` blanks the proxy variables the runtime set. They are a
            convenience for the CLI, not the boundary, and an executor that
            strips them is the case that matters: it must reach less, not more.
            """
            environment = None
            if unset_proxy:
                environment = dict.fromkeys(
                    ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"],
                    "",
                )
            return executor.exec_run(command, user="root", environment=environment)

        # --- positive control: the ledger works ------------------------------
        allowed_write = scenario["control"].exec_run(
            ["curl", "-sS", "-m", "10", "-X", "POST", f"http://{app_ip}:{APP_PORT}/orders", "-d", "{}"]
        )
        assert allowed_write.exit_code == 0, allowed_write.output
        assert b"created" in allowed_write.output

        # --- positive control: the allowed directions are open ---------------
        _wait_for_port(daemon, executor.id, "capability", CAPABILITY_PORT)
        capability_call = in_executor(
            [
                "curl",
                "-sS",
                "-m",
                "10",
                "-X",
                "POST",
                f"http://capability:{CAPABILITY_PORT}/qa/call",
                "-H",
                "Authorization: Bearer run-token",
                "-d",
                '{"tool": "http_get", "args": {"path": "/"}}',
            ]
        )
        assert capability_call.exit_code == 0, capability_call.output
        assert b'"status": 200' in capability_call.output

        tunnel = in_executor(["python3", "-c", TUNNEL, f"{egress.proxy_host}:3128", f"backend:{BACKEND_PORT}"])
        assert tunnel.exit_code == 0, tunnel.output
        assert b"model-backend-answered" in tunnel.output

        # --- the writes the executor must not be able to make ----------------
        # Directly, with the run's proxy configuration stripped — the case that
        # matters, because a shell can strip it. There is no route from this
        # container to that address at all, by IP or by name, from a shell
        # client or a Python one.
        direct = []
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for target in (f"http://{app_ip}:{APP_PORT}/orders", f"http://app:{APP_PORT}/orders"):
                direct.append(
                    in_executor(
                        ["curl", "-sS", "-m", "8", "-X", method, target, "-d", '{"qa": "write"}'],
                        unset_proxy=True,
                    )
                )
                direct.append(
                    in_executor(
                        [
                            "python3",
                            "-c",
                            "import sys, urllib.request\n"
                            "r = urllib.request.Request(sys.argv[1], data=b'{}', method=sys.argv[2])\n"
                            "print(urllib.request.urlopen(r, timeout=8).status)",
                            target,
                            method,
                        ],
                        unset_proxy=True,
                    )
                )
        for attempt in direct:
            assert attempt.exit_code != 0, f"a write attempt reached something: {attempt.output!r}"

        # Through the one door the run does have, which answers — and refuses.
        # These exit 0 because the *proxy* replied, so the assertion is what it
        # replied, and below, what the application never received.
        tunnelled = in_executor(["python3", "-c", TUNNEL, f"{egress.proxy_host}:3128", f"{app_ip}:{APP_PORT}"])
        assert tunnelled.exit_code != 0
        assert b"403" in tunnelled.output, tunnelled.output

        forwarded = in_executor(
            [
                "curl",
                "-sS",
                "-m",
                "8",
                "-X",
                "POST",
                "--proxy",
                f"http://{egress.proxy_host}:3128",
                f"http://{app_ip}:{APP_PORT}/orders",
                "-d",
                "{}",
            ]
        )
        assert b"CONNECT only" in forwarded.output, forwarded.output

        for attempt in [*direct, tunnelled, forwarded]:
            assert b"created" not in attempt.output, attempt.output

        # --- the application's own ledger ------------------------------------
        ledger = scenario["control"].exec_run(["curl", "-sS", "-m", "10", f"http://{app_ip}:{APP_PORT}/__recorded"])
        assert ledger.exit_code == 0, ledger.output
        recorded = json.loads(ledger.output.decode().strip().splitlines()[-1])
        assert recorded == ["POST /orders"], (
            f"the application received requests other than the control write: {recorded}"
        )
    finally:
        if executor is not None:
            executor.remove(force=True)
        asyncio.run(qa_egress.tear_down(wrapper, worker_id))
