"""Exercise the Claude image's installer shell step against a local HTTP server."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import subprocess
import threading

import pytest

DOCKERFILE = (
    Path(__file__).parents[2] / "services/worker-manager/images/worker-base-claude/Dockerfile"
)
INSTALLER_URL = "https://claude.ai/install.sh"


def _installer_command(tmp_path, url):
    dockerfile = DOCKERFILE.read_text()
    run = re.search(r"^RUN installer=.*?(?=\n\n|\n#)", dockerfile, re.MULTILINE | re.DOTALL)
    assert run is not None
    command = run.group().removeprefix("RUN ").replace("\\\n", "")
    command = command.replace(INSTALLER_URL, url)
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
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        marker = tmp_path / "executed"
        env = os.environ.copy()
        env.update(CLAUDE_CODE_VERSION="2.1.278", INSTALL_MARKER=str(marker))
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
