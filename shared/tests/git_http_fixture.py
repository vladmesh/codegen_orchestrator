"""Offline GitHub transport double; Git still serializes its own real config."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
from urllib.parse import urlsplit


class GitHTTPFixture:
    """Serve a real bare repository through git-http-backend with canary Basic auth."""

    def __init__(self, root: Path, token: str):
        self.root = root
        self.token = token
        self.headers = []
        self.remote = root / "remote.git"
        self.git = shutil.which("git")
        assert self.git
        self.run("init", "--bare", "--initial-branch=main", str(self.remote))
        self.run("-C", str(self.remote), "config", "http.receivepack", "true")
        seed = root / "seed"
        self.run("init", "--initial-branch=main", str(seed))
        self.run("-C", str(seed), "config", "user.name", "Fixture")
        self.run("-C", str(seed), "config", "user.email", "fixture@example.test")
        (seed / "README.md").write_text("fixture product\n")
        (seed / "Makefile").write_text("setup:\n\tgit config core.hooksPath .githooks\n")
        hooks = seed / ".githooks"
        hooks.mkdir()
        (hooks / "pre-push").write_text("#!/bin/sh\necho product-hook >> hook-ran\n")
        (hooks / "pre-push").chmod(0o755)
        self.run("-C", str(seed), "add", ".")
        self.run("-C", str(seed), "commit", "-m", "fixture")
        self.run("-C", str(seed), "push", str(self.remote), "main")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):  # noqa: N802 - HTTP handler interface
                self.answer()

            def do_POST(self):  # noqa: N802 - HTTP handler interface
                self.answer()

            def answer(self):
                authorization = self.headers.get("Authorization", "")
                fixture.headers.append(authorization)
                expected = (
                    "Basic " + base64.b64encode(f"x-access-token:{fixture.token}".encode()).decode()
                )
                if authorization != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="fixture"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                url = urlsplit(self.path)
                prefix = "/org/repo.git" if url.path.startswith("/org/repo.git") else "/org/repo"
                environment = {
                    **os.environ,
                    "GIT_PROJECT_ROOT": str(root),
                    "GIT_HTTP_EXPORT_ALL": "1",
                    "PATH_INFO": "/remote.git" + url.path.removeprefix(prefix),
                    "REQUEST_METHOD": self.command,
                    "QUERY_STRING": url.query,
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "REMOTE_USER": "fixture",
                    "HTTP_GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
                }
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                result = subprocess.run(
                    [fixture.git, "http-backend"],
                    input=body,
                    capture_output=True,
                    env=environment,
                    timeout=10,
                    check=True,
                )
                header, payload = result.stdout.split(b"\r\n\r\n", 1)
                fields = [line.decode().split(": ", 1) for line in header.split(b"\r\n")]
                status = next(
                    (int(value.split()[0]) for key, value in fields if key == "Status"), 200
                )
                self.send_response(status)
                for key, value in fields:
                    if key != "Status":
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.argv_path = root / "git-argv.jsonl"
        config = root / "global.gitconfig"
        self.run(
            "config", "--file", str(config), f"url.{self.url}.insteadOf", "https://github.com/"
        )
        binaries = root / "bin"
        binaries.mkdir()
        # Endpoint substitution only: argv and workspace config remain Git's.
        # The scoped HTTP header follows the substituted fixture endpoint.
        wrapper = binaries / "git"
        wrapper.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"with open({str(self.argv_path)!r}, 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "env = os.environ.copy()\n"
            "for key, value in list(env.items()):\n"
            "    if key.startswith('GIT_CONFIG_KEY_') and value == 'http.https://github.com/.extraheader':\n"
            f"        env[key] = 'http.{self.url}.extraheader'\n"
            f"os.execve({self.git!r}, [{self.git!r}, *sys.argv[1:]], env)\n"
        )
        wrapper.chmod(0o755)
        self.environment = {
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }

    def run(self, *args):
        return subprocess.run(
            [self.git, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        ).stdout.strip()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def argv(self):
        return [json.loads(line) for line in self.argv_path.read_text().splitlines()]
