"""Exercise the Claude image's installer shell step against a local HTTP server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

import pytest

# Every test here starts processes: CI runs this file, the host profile skips it.
pytestmark = pytest.mark.subprocess

DOCKERFILE = (
    Path(__file__).parents[2] / "services/worker-manager/images/worker-base-claude/Dockerfile"
)
INSTALLER_URL = "https://claude.ai/install.sh"

# curl cannot retry without a real wait: `--retry-delay 0` selects its default 1, 2, 4 s
# backoff and `Retry-After: 0` is ignored. So the step's `curl` is this shim: it keeps the
# step's own retry flags (`--retry N`, `--retry-delay D`, `--retry-connrefused`), runs the
# real curl once per attempt against the local server, and waits D seconds between
# attempts, which is 0 here. A transient failure is what curl's `--retry` retries on:
# an HTTP error answer (exit 22) or, with `--retry-connrefused`, a refused connection (7).
CURL_SHIM = """#!/usr/bin/env bash
retries=0 delay=0 connrefused=0 args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --retry) retries=$2; shift 2 ;;
        --retry-delay) delay=$2; shift 2 ;;
        --retry-connrefused) connrefused=1; shift ;;
        *) args+=("$1"); shift ;;
    esac
done
for ((attempt = 0; ; attempt++)); do
    status=0
    "$REAL_CURL" "${args[@]}" || status=$?
    transient=0
    [ "$status" -eq 22 ] && transient=1
    [ "$status" -eq 7 ] && [ "$connrefused" = 1 ] && transient=1
    if [ "$status" -eq 0 ] || [ "$transient" = 0 ] || [ "$attempt" -ge "$retries" ]; then
        exit "$status"
    fi
    sleep "$delay"
done
"""


def _installer_command(tmp_path, url):
    dockerfile = DOCKERFILE.read_text()
    run = re.search(r"^RUN installer=.*?(?=\n\n|\n#)", dockerfile, re.MULTILINE | re.DOTALL)
    assert run is not None
    command = run.group().removeprefix("RUN ").replace("\\\n", "")
    command = command.replace(INSTALLER_URL, url)
    # The retry count is what is under test, not a real wait between attempts.
    assert "curl --retry 3 " in command
    command = command.replace("curl --retry 3 ", "curl --retry 3 --retry-delay 0 ")
    cli = tmp_path / "claude"
    cli.write_text("#!/bin/sh\necho 2.1.278\n")
    cli.chmod(0o755)
    return command.replace("/home/worker/.local/bin/claude", str(cli))


def _run_fetch(tmp_path, statuses, *, empty=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status = statuses[min(len(requests) - 1, len(statuses) - 1)]
            body = b'printf installed > "$INSTALL_MARKER"\n' if status == 200 and not empty else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        marker = tmp_path / "executed"
        shims = tmp_path / "shims"
        shims.mkdir()
        (shims / "curl").write_text(CURL_SHIM)
        (shims / "curl").chmod(0o755)
        env = os.environ.copy()
        env.update(
            CLAUDE_CODE_VERSION="2.1.278",
            INSTALL_MARKER=str(marker),
            REAL_CURL=shutil.which("curl"),
            PATH=f"{shims}:{env['PATH']}",
        )
        command = _installer_command(tmp_path, f"http://127.0.0.1:{server.server_port}/install.sh")
        result = subprocess.run(
            ["bash", "-c", command],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result, requests, marker
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_transient_installer_fetch_failure_retries_then_executes(tmp_path):
    result, requests, marker = _run_fetch(tmp_path, [503, 200])

    assert result.returncode == 0, result.stderr
    assert len(requests) == 2
    assert marker.read_text() == "installed"


@pytest.mark.parametrize("statuses,empty", [([503], False), ([200], True)])
def test_failed_or_empty_installer_fetch_never_executes(tmp_path, statuses, empty):
    result, requests, marker = _run_fetch(tmp_path, statuses, empty=empty)

    assert result.returncode != 0
    assert len(requests) == (4 if statuses == [503] else 1)
    assert not marker.exists()


INFRA_CAUSE = "CI-INFRA-CAUSE=claude-installer-fetch"


def test_exhausted_installer_fetch_names_the_infrastructure_cause(tmp_path):
    """scripts/ci-infra.sh watch maps this line to the CI infrastructure marker."""
    result, requests, marker = _run_fetch(tmp_path, [503])

    assert result.returncode != 0
    assert len(requests) == 4
    assert INFRA_CAUSE in result.stderr.splitlines()
    assert not marker.exists()


@pytest.mark.parametrize("statuses,empty", [([503, 200], False), ([200], True)])
def test_a_fetch_that_answered_names_no_infrastructure_cause(tmp_path, statuses, empty):
    """An empty script, or a retry that recovered, is not a fetch that never answered."""
    result, _, _ = _run_fetch(tmp_path, statuses, empty=empty)

    assert INFRA_CAUSE not in result.stdout + result.stderr
