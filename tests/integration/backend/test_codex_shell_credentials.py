"""Shipped Codex shell policy, without a thread, model request or paid turn."""

from pathlib import Path
import re
from uuid import uuid4

import pytest

from shared.contracts.queues.worker import AgentType, CreateWorkerCommand, WorkerCapability

from .conftest import (
    REDIS_STREAM_COMMANDS,
    REDIS_STREAM_DEV_RESPONSES,
    scaffolded_worker_config,
    wait_for_create_response,
)
from .test_worker_execution import _ownership

PROBE = r'''
import json, os, select, shlex, subprocess, tempfile, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from worker_wrapper.credentials import repository_from_origin
from worker_wrapper.runners.codex import CodexRunner
from worker_wrapper.wrapper import build_agent_subprocess_env

assert subprocess.check_output(["codex", "--version"], text=True).strip() == (
    "codex-cli " + os.environ["CODEGEN_TEST_EXPECTED_CLI"]
)
repository = repository_from_origin()
current = ["synthetic-first"]
requests = []
class Broker(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/workers/fixture/github/credential"
        assert self.headers["X-Worker-Broker-Token"] == "synthetic-broker-identity"
        assert payload == {"repository": repository}
        requests.append(payload)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"token": current[0]}).encode())
        current[0] = "synthetic-after-expiry"
    def log_message(self, *args):
        pass
broker = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
thread = Thread(target=broker.serve_forever, daemon=True)
thread.start()
try:
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        (home / ".gitconfig").write_text(
            '[credential "https://github.com"]\n'
            'helper = /usr/local/bin/git-credential-codegen\nuseHttpPath = true\n'
        )
        profile = home / "codex"
        profile.mkdir()
        # The runner must override a mounted policy that would drop the broker.
        (profile / "config.toml").write_text(
            '[shell_environment_policy]\ninherit = "none"\nignore_default_excludes = false\n'
        )
        environment = build_agent_subprocess_env({
            **os.environ, "HOME": str(home), "CODEX_HOME": str(profile),
            "WORKER_BROKER_URL": f"http://127.0.0.1:{broker.server_port}",
            "WORKER_BROKER_TOKEN": "synthetic-broker-identity", "WORKER_ID": "fixture",
            "GITHUB_TOKEN": "synthetic-forbidden", "GH_TOKEN": "synthetic-forbidden",
        })
        assert "GITHUB_TOKEN" not in environment and "GH_TOKEN" not in environment
        command = CodexRunner().build_command("unused")
        overrides = [command[i + 1] for i, arg in enumerate(command) if arg == "-c"]
        # command/exec uses the pinned CLI's same create_env implementation as
        # shell tools. No thread/start or turn/start request is ever sent.
        process = subprocess.Popen(
            ["codex", *[arg for value in overrides for arg in ("-c", value)], "app-server"],
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()
        pending = b""
        def receive(request_id):
            global pending
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if b"\n" not in pending:
                    remaining = max(0, deadline - time.monotonic())
                    assert select.select([process.stdout], [], [], remaining)[0]
                    chunk = os.read(process.stdout.fileno(), 65536)
                    assert chunk, "CLI protocol ended"
                    pending += chunk
                    continue
                line, pending = pending.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") == request_id:
                    assert "error" not in message, "CLI request refused"
                    return message["result"]
            raise AssertionError("CLI protocol timed out")
        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "credential_probe", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }})
            receive(1)
            send({"method": "initialized"})
            shell = """import os, subprocess, sys
from pathlib import Path
assert all(name in os.environ for name in ("WORKER_BROKER_URL", "WORKER_BROKER_TOKEN", "WORKER_ID"))
blocked = ("GITHUB_TOKEN", "GH_TOKEN", "CODEX_API_KEY", "QA_CAPABILITY_TOKEN")
assert not any(name in os.environ for name in blocked)
query = """ + repr(f"protocol=https\nhost=github.com\npath={repository}.git\n\n") + """
for token in ("synthetic-first", "synthetic-after-expiry"):
    result = subprocess.run(["git", "credential", "fill"], input=query, text=True,
                            capture_output=True, check=True, timeout=15)
    assert f"password={token}\\n" in result.stdout
    assert not result.stderr
    for operation in ("approve", "reject"):
        subprocess.run(["git", "credential", operation], input=result.stdout + "\\n",
                       text=True, capture_output=True, check=True, timeout=15)
home = Path(os.environ["HOME"])
assert not (home / ".git-credentials").exists()
assert not (home / ".config/codegen/git-credentials").exists()
assert not (home / ".config/gh/hosts.yml").exists()
assert "synthetic-" not in (home / ".gitconfig").read_text()
sys.stdout.write("codex-helper-shell-passed\\n")
"""
            send({"id": 2, "method": "command/exec", "params": {
                "command": ["/bin/sh", "-c", "python3 -c " + shlex.quote(shell)],
                "cwd": "/workspace", "timeoutMs": 25000,
                "sandboxPolicy": {"type": "dangerFullAccess"},
            }})
            result = receive(2)
            assert result["exitCode"] == 0, "Codex helper shell failed"
            assert result["stdout"] == "codex-helper-shell-passed\n"
            assert not result["stderr"]
            assert requests == [{"repository": repository}] * 2
        finally:
            process.terminate()
            process.wait(timeout=5)
finally:
    broker.shutdown()
    broker.server_close()
    thread.join(timeout=5)
'''


@pytest.mark.integration
@pytest.mark.asyncio
async def test_shipped_codex_shell_retains_scoped_broker_identity(
    redis_client, docker_client, scaffolded_workspace
):
    request_id = f"codex-policy-{uuid4().hex[:8]}"
    command = CreateWorkerCommand(
        request_id=request_id,
        config=scaffolded_worker_config(
            scaffolded_workspace,
            name=request_id,
            worker_type="developer",
            agent_type=AgentType.CODEX,
            auth_mode="api_key",
            api_key="sk-codex-synthetic-no-model",
            instructions="No model turn is requested.",
            allowed_commands=[],
            capabilities=[WorkerCapability.GIT],
            ownership=_ownership(),
        ),
    )
    await redis_client.xadd(REDIS_STREAM_COMMANDS, {"data": command.model_dump_json()})
    result = await wait_for_create_response(
        redis_client, REDIS_STREAM_DEV_RESPONSES, request_id=request_id
    )
    assert result.success, result.error
    dockerfile = (
        Path(__file__).parents[3] / "services/worker-manager/images/worker-base-codex/Dockerfile"
    )
    version = re.search(r"^ARG CODEX_CLI_VERSION=(\S+)$", dockerfile.read_text(), re.MULTILINE)[1]
    container = docker_client.containers.get(f"worker-{result.worker_id}")
    code, output = container.exec_run(
        ["python3", "-c", PROBE], environment={"CODEGEN_TEST_EXPECTED_CLI": version}
    )
    assert code == 0, f"Pinned Codex shell probe failed: {output.decode()}"
