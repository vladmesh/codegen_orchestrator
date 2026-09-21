"""Worker-mode compose without an edit of the product's Makefile.

The orchestrator used to append its own `worker-start` / `worker-stop` recipes to the
product's **tracked** `Makefile`. The recipes worked, and the edit travelled: a worker's
`git add -A` published them, and the merged product's CI then logged `overriding recipe
for target worker-start`. The mechanism is now an environment value for the seam the kit
already has — `DOCKER_COMPOSE ?= docker compose` — pointing at a stand-in program the
wrapper writes outside the checkout.

So these tests run the *pinned kit's own* Makefile: what has to keep working is
`make worker-start` and `make worker-stop` in a worker container, with the same request
bodies the injected recipes sent, while local mode keeps the published ports it selects
its own compose files for.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

import pytest
from worker_wrapper.compose_proxy import (
    COMPOSE_COMMAND_ENV,
    compose_proxy_supported,
    install_compose_proxy,
)
from worker_wrapper.wrapper import WorkerWrapper

from scripts.template_pin import TEMPLATE_PIN

KIT_MAKEFILE = TEMPLATE_PIN.fixture_path() / "Makefile"


@pytest.fixture
def checkout(tmp_path):
    """The product checkout, kept apart from the container's HOME."""
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def wrapper(tmp_path, checkout, monkeypatch):
    """A wrapper whose workspace is a copy of the pinned kit's Makefile."""
    monkeypatch.setattr("worker_wrapper.wrapper.WORKSPACE_DIR", str(checkout))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    config = type(
        "Config",
        (),
        {
            "broker_url": "http://worker-broker:8001",
            "broker_token": "x" * 43,
            "worker_id": "worker-42",
            "agent_type": "claude",
            "worker_type": "developer",
            "poll_interval_ms": 500,
            "subprocess_timeout_seconds": 300,
            "http_server_port": 9090,
            "model_dump": lambda self: {},
        },
    )()

    w = WorkerWrapper.__new__(WorkerWrapper)
    w.config = config
    w._compose_proxy_path = None
    return w


class ComposeProxyStub:
    """The wrapper's /infra/compose endpoint, recording exactly what reaches it."""

    def __init__(self, answer):
        self.requests: list[dict] = []
        self.answer = answer
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's spelling
                length = int(self.headers.get("Content-Length", 0))
                stub.requests.append(
                    {"path": self.path, "body": json.loads(self.rfile.read(length) or b"{}")}
                )
                status, payload = stub.answer
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def proxy_stub():
    stubs: list[ComposeProxyStub] = []

    def make(answer=(200, {"exit_code": 0, "stdout": "", "stderr": ""})):
        stub = ComposeProxyStub(answer)
        stubs.append(stub)
        return stub

    yield make
    for stub in stubs:
        stub.close()


def run_make(workspace: Path, target: str, proxy_path: str, *make_args: str, docker_log=None):
    """Run a kit target the way a worker container does: proxy in the environment."""
    env = os.environ | {COMPOSE_COMMAND_ENV: proxy_path}
    if docker_log is not None:
        fake_bin = workspace / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        docker = fake_bin / "docker"
        docker.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" >> "{docker_log}"\n')
        docker.chmod(0o755)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["make", "-s", target, *make_args],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestTurnPreparationLeavesTheProductAlone:
    def test_the_products_makefile_is_never_written(self, wrapper, checkout):
        """The whole defect in one assertion: preparation must not touch the kit file."""
        makefile = checkout / "Makefile"
        shutil.copy(KIT_MAKEFILE, makefile)
        before = makefile.read_bytes()

        wrapper._install_compose_proxy()

        assert makefile.read_bytes() == before
        assert "# --- orchestrator overrides ---" not in makefile.read_text()

    def test_the_proxy_program_lives_outside_the_checkout(self, wrapper, checkout):
        shutil.copy(KIT_MAKEFILE, checkout / "Makefile")

        wrapper._install_compose_proxy()

        proxy = Path(wrapper._compose_proxy_path)
        assert proxy.is_file() and os.access(proxy, os.X_OK)
        assert checkout not in proxy.parents, "the checkout may hold nothing of the orchestrator"

    def test_the_agent_environment_names_the_proxy(self, wrapper, checkout):
        shutil.copy(KIT_MAKEFILE, checkout / "Makefile")

        wrapper._install_compose_proxy()
        agent_env = wrapper._build_agent_env({"PATH": "/usr/bin", "HOME": str(checkout)})

        assert agent_env[COMPOSE_COMMAND_ENV] == wrapper._compose_proxy_path

    def test_an_agent_without_a_proxy_gets_no_compose_command(self, wrapper):
        agent_env = wrapper._build_agent_env({"PATH": "/usr/bin"})

        assert COMPOSE_COMMAND_ENV not in agent_env

    def test_missing_makefile_fails_workspace_preparation(self, wrapper, checkout):
        """A worker without a Makefile cannot safely run worker-mode targets."""
        with pytest.raises(RuntimeError, match="Makefile is missing"):
            wrapper._install_compose_proxy()

        assert not (checkout / "Makefile").exists()

    def test_a_makefile_that_hardcodes_compose_is_refused(self, wrapper, checkout):
        """Without the `$(DOCKER_COMPOSE)` seam the targets would need a Docker socket."""
        (checkout / "Makefile").write_text("worker-start:\n\tdocker compose up -d\n")

        with pytest.raises(RuntimeError, match=r"\$\(DOCKER_COMPOSE\)"):
            wrapper._install_compose_proxy()

    def test_the_pinned_kit_keeps_the_seam(self):
        """If the pin ever drops `$(DOCKER_COMPOSE)`, this is where it is noticed."""
        assert compose_proxy_supported(str(KIT_MAKEFILE))
        assert "DOCKER_COMPOSE ?=" in KIT_MAKEFILE.read_text()


class TestWorkerModeTargetsReachTheProxy:
    @pytest.fixture
    def workspace(self, tmp_path):
        shutil.copy(KIT_MAKEFILE, tmp_path / "Makefile")
        return tmp_path

    def test_worker_start_sends_the_same_request_body(self, workspace, proxy_stub, monkeypatch):
        monkeypatch.setenv("HOME", str(workspace / "home"))
        stub = proxy_stub()
        proxy = install_compose_proxy(stub.port)

        result = run_make(workspace, "worker-start", proxy, "svc=db")

        assert result.returncode == 0, result.stderr
        assert stub.requests == [
            {
                "path": "/infra/compose",
                "body": {"args": ["up", "-d", "--build", "--wait", "db"], "cwd": "."},
            }
        ]

    def test_worker_stop_is_project_scoped_without_volumes(
        self, workspace, proxy_stub, monkeypatch
    ):
        monkeypatch.setenv("HOME", str(workspace / "home"))
        stub = proxy_stub()
        proxy = install_compose_proxy(stub.port)

        result = run_make(workspace, "worker-stop", proxy)

        assert result.returncode == 0, result.stderr
        assert stub.requests[0]["body"] == {"args": ["down", "--remove-orphans"], "cwd": "."}
        assert "--volumes" not in stub.requests[0]["body"]["args"]

    def test_local_mode_keeps_its_published_ports(self, workspace, proxy_stub, monkeypatch):
        """dev-start selects the local compose files and must stay a real Docker call."""
        monkeypatch.setenv("HOME", str(workspace / "home"))
        stub = proxy_stub()
        proxy = install_compose_proxy(stub.port)
        docker_log = workspace / "docker.log"

        result = run_make(workspace, "dev-start", proxy, docker_log=docker_log)

        assert result.returncode == 0, result.stderr
        assert stub.requests == [], "local mode must not be proxied — the proxy strips ports"
        assert docker_log.read_text().split("\n")[:5] == [
            "compose",
            "-f",
            "infra/compose.base.yml",
            "-f",
            "infra/compose.dev.yml",
        ]
        assert "infra/compose.local.yml" in docker_log.read_text()

    def test_local_mode_stop_is_not_proxied_either(self, workspace, proxy_stub, monkeypatch):
        monkeypatch.setenv("HOME", str(workspace / "home"))
        stub = proxy_stub()
        proxy = install_compose_proxy(stub.port)
        docker_log = workspace / "docker.log"

        result = run_make(workspace, "dev-stop", proxy, docker_log=docker_log)

        assert result.returncode == 0, result.stderr
        assert stub.requests == []
        assert "infra/compose.local.yml" in docker_log.read_text()

    @pytest.mark.parametrize(
        ("answer", "should_succeed", "stderr_fragment"),
        [
            ((200, {"exit_code": 0, "stderr": "safe proxy output"}), True, "safe proxy output"),
            ((200, {"exit_code": 7, "stderr": "compose failed"}), False, "compose failed"),
            ((200, b"not json"), False, "compose proxy request failed"),
            ((200, {"stderr": "missing exit code"}), False, "missing exit code"),
            ((403, {"detail": "denied"}), False, "denied"),
        ],
    )
    def test_proxy_failures_fail_the_target(
        self, workspace, proxy_stub, monkeypatch, answer, should_succeed, stderr_fragment
    ):
        """A refused or unreadable proxy answer is a failed target, never a silent pass."""
        monkeypatch.setenv("HOME", str(workspace / "home"))
        stub = proxy_stub(answer)
        proxy = install_compose_proxy(stub.port)

        result = run_make(workspace, "worker-start", proxy)

        assert (result.returncode == 0) is should_succeed
        assert stderr_fragment in result.stderr

    def test_an_unreachable_wrapper_fails_the_target(self, workspace, monkeypatch, proxy_stub):
        monkeypatch.setenv("HOME", str(workspace / "home"))
        stub = proxy_stub()
        port = stub.port
        stub.close()
        proxy = install_compose_proxy(port)

        result = run_make(workspace, "worker-start", proxy)

        assert result.returncode != 0
        assert "compose proxy request failed" in result.stderr
