"""Offline broker and installed-style helper for native credential tests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread


class WorkerCredentialFixture:
    def __init__(self, root: Path):
        self.token = "synthetic-first-token"  # noqa: S105 - harmless fixture
        self.requests = []
        self.refused = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append(payload)
                allowed = (
                    self.path == "/v1/workers/fixture/github/credential"
                    and self.headers.get("X-Worker-Broker-Token")
                    == "synthetic-broker-credential-32-characters"
                    and payload == {"repository": "org/repo"}
                    and not fixture.refused
                )
                self.send_response(200 if allowed else 403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"token": fixture.token} if allowed else {}).encode())

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        repo = Path(__file__).resolve().parents[2]
        self.helper = root / "git-credential-codegen"
        self.helper.write_text(
            f"#!{sys.executable} -I\nimport sys\n"
            f"sys.path[:0] = {[str(repo), str(repo / 'packages/worker-wrapper/src')]!r}\n"
            "from worker_wrapper.credentials import git_main\ngit_main()\n"
        )
        self.helper.chmod(0o755)
        self.environment = {
            "WORKER_BROKER_URL": f"http://127.0.0.1:{self.server.server_port}",
            "WORKER_BROKER_TOKEN": "synthetic-broker-credential-32-characters",
            "WORKER_ID": "fixture",
        }

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
