"""Offline GitHub transport double; Git still serializes its own real config."""

import base64
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
from threading import Thread
from urllib.parse import urlsplit


class GitHTTPFixture:
    """Serve a real bare repository through git-http-backend with canary Basic auth."""

    def __init__(self, root: Path, token: str, *, github_proxy: bool = False):
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
        self.tls_context = None
        if github_proxy:
            # A local CONNECT endpoint keeps Git's real github.com credential
            # scope and URL intact; no helper/config or credential is mocked.
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "github.com")])
            now = datetime.now(UTC)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("github.com")]), False)
                .sign(key, hashes.SHA256())
            )
            self.ca_path = root / "fixture-ca.pem"
            self.ca_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path = root / "fixture-key.pem"
            key_path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            self.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.tls_context.load_cert_chain(self.ca_path, key_path)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):  # noqa: N802 - HTTP handler interface
                self.answer()

            def do_POST(self):  # noqa: N802 - HTTP handler interface
                self.answer()

            def do_CONNECT(self):  # noqa: N802 - HTTP handler interface
                if fixture.tls_context is None or self.path != "github.com:443":
                    self.send_error(403)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                with fixture.tls_context.wrap_socket(
                    self.connection, server_side=True
                ) as connection:
                    Handler(connection, self.client_address, self.server)

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
        self.config_path = config
        if github_proxy:
            self.run("config", "--file", str(config), "http.https://github.com.proxy", self.url)
            self.run(
                "config",
                "--file",
                str(config),
                "http.https://github.com.sslCAInfo",
                str(self.ca_path),
            )
        else:
            self.run(
                "config", "--file", str(config), f"url.{self.url}.insteadOf", "https://github.com/"
            )
        binaries = root / "bin"
        binaries.mkdir()
        # Endpoint substitution only: argv and workspace config remain Git's.
        # The scoped HTTP header follows the substituted fixture endpoint.
        wrapper = binaries / "git"
        self.transport_path = root / "git-transport.jsonl"
        header_scope = (
            "http.https://github.com/.extraheader"
            if github_proxy
            else f"http.{self.url}.extraheader"
        )
        wrapper.write_text(
            f"#!{sys.executable} -I\nimport json, os, sys\n"
            f"with open({str(self.argv_path)!r}, 'a') as stream:\n"
            "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "env = os.environ.copy()\n"
            "for name, value in env.items():\n"
            "    if name.startswith('GIT_CONFIG_VALUE_') and value.startswith('store --file='):\n"
            "        path = value.removeprefix('store --file=')\n"
            "        with open(path) as stream:\n"
            "            present = bool(stream.read())\n"
            f"        with open({str(self.transport_path)!r}, 'a') as stream:\n"
            "            stream.write(json.dumps({'path': path, "
            "'mode': os.stat(path).st_mode & 0o777, "
            "'parent_mode': os.stat(os.path.dirname(path)).st_mode & 0o777, "
            "'present': present}) + '\\n')\n"
            "for key, value in list(env.items()):\n"
            "    if key.startswith('GIT_CONFIG_KEY_') and value == 'http.https://github.com/.extraheader':\n"
            f"        env[key] = {header_scope!r}\n"
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

    def transports(self):
        return [json.loads(line) for line in self.transport_path.read_text().splitlines()]
