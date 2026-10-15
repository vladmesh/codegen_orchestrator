"""The admin proxy resolves upstreams per request and keeps each route's upstream URI.

nginx resolves a host named in `proxy_pass` once at startup and exits on an unknown name, so
the admin UI would die whenever worker-manager, grafana or api starts later. The config is
parsed here and routed the way nginx routes it: exact location first, then the longest prefix,
`rewrite ... break`, and `proxy_pass`, whose URI part replaces the matched prefix when the host
is literal and the whole URI when it is a variable; without a URI part the URI passes as is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import socket
import subprocess
import time
from uuid import uuid4

import httpx
import pytest

ADMIN_FRONTEND = Path(__file__).parents[2] / "services/admin-frontend"
NGINX_CONF = ADMIN_FRONTEND / "nginx.conf"


@dataclass(frozen=True)
class Upstream:
    host: str
    uri: str
    headers: dict[str, str]
    http_version: str | None


def _directives(text: str) -> tuple[list[list[str]], dict[tuple[str, ...], list[list[str]]]]:
    """Server-level directives and each location's directives, as token lists."""
    words = re.findall(r'"[^"]*"|\'[^\']*\'|[{};]|[^\s{};]+', re.sub(r"#[^\n]*", "", text))
    server: list[list[str]] = []
    locations: dict[tuple[str, ...], list[list[str]]] = {}
    blocks: list[tuple[str, ...]] = []
    current: list[str] = []
    for word in words:
        if word == "{":
            blocks.append(tuple(current))
            if current[0] == "location":
                locations[tuple(current[1:])] = []
            current = []
        elif word == "}":
            blocks.pop()
        elif word == ";":
            if blocks and blocks[-1][0] == "location":
                locations[blocks[-1][1:]].append(current)
            elif blocks and blocks[-1][0] == "server":
                server.append(current)
            current = []
        else:
            current.append(word.strip("\"'"))
    return server, locations


def _route(path: str) -> Upstream:
    server, locations = _directives(NGINX_CONF.read_text())
    variables = {d[1]: d[2] for d in server if d[0] == "set"}
    prefix = path
    location = locations.get(("=", path))
    if location is None:
        prefix = max((k[0] for k in locations if len(k) == 1 and path.startswith(k[0])), key=len)
        location = locations[(prefix,)]
    uri = path
    for directive in location:
        if directive[0] == "rewrite" and directive[3] == "break":
            uri = re.sub(directive[1], re.sub(r"\$(\d)", r"\\g<\1>", directive[2]), uri)
            break
    (proxy_pass,) = [d[1] for d in location if d[0] == "proxy_pass"]
    host, target_uri = re.fullmatch(r"http://([^/]+)(/.*)?", proxy_pass).groups()
    if host.startswith("$"):
        host = variables[host]
    elif target_uri is not None:
        target_uri += path[len(prefix) :]
    return Upstream(
        host=host,
        uri=target_uri or uri,
        headers={d[1]: d[2] for d in location if d[0] == "proxy_set_header"},
        http_version=next((d[1] for d in location if d[0] == "proxy_http_version"), None),
    )


def test_every_upstream_resolves_through_docker_dns_at_request_time():
    server, locations = _directives(NGINX_CONF.read_text())
    proxied = [
        d[1] for directives in locations.values() for d in directives if d[0] == "proxy_pass"
    ]

    assert ["resolver", "127.0.0.11", "valid=10s", "ipv6=off"] in server
    assert len(proxied) == 5
    assert all(re.fullmatch(r"https?://\$\w+(/.*)?", target) for target in proxied), proxied


@pytest.mark.parametrize(
    ("path", "host", "uri"),
    [
        ("/api/projects/p1", "api:8000", "/api/projects/p1"),
        ("/api/debug/queues", "api:8000", "/api/debug/queues"),
        ("/wm-api/workers/w1/logs", "worker-manager:8000", "/api/introspect/workers/w1/logs"),
        ("/health", "api:8000", "/health"),
        ("/grafana/d/overview", "grafana:3000", "/grafana/d/overview"),
        ("/grafana/api/live/ws", "grafana:3000", "/grafana/api/live/ws"),
    ],
)
def test_each_route_reaches_the_same_upstream_uri(path, host, uri):
    upstream = _route(path)

    assert (upstream.host, upstream.uri) == (host, uri)


def test_grafana_live_route_keeps_the_websocket_upgrade():
    upstream = _route("/grafana/api/live/ws")

    assert upstream.http_version == "1.1"
    assert upstream.headers["Upgrade"] == "$http_upgrade"
    assert upstream.headers["Connection"] == "upgrade"


def _docker(*args: str, check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess:
    docker = shutil.which("docker")
    assert docker is not None
    return subprocess.run(
        [docker, *args], check=check, capture_output=True, text=True, timeout=timeout
    )


@pytest.mark.docker
@pytest.mark.timeout(150)
def test_nginx_loads_and_stays_up_without_any_upstream(tmp_path):
    """On a network with no api, worker-manager or grafana, proxied routes answer 502 or 504."""
    (image,) = re.findall(r"^FROM (nginx:\S+)", (ADMIN_FRONTEND / "Dockerfile").read_text(), re.M)
    htpasswd = tmp_path / "htpasswd"
    htpasswd.write_text("probe:{PLAIN}probe\n")
    name = f"admin-nginx-probe-{uuid4().hex[:12]}"
    mounts = [
        *("-v", f"{NGINX_CONF}:/etc/nginx/conf.d/default.conf:ro"),
        *("-v", f"{htpasswd}:/etc/nginx/.htpasswd:ro"),
    ]
    nginx = ["--network", name, *mounts, "--entrypoint", "nginx", image]
    _docker("pull", image, timeout=60)
    _docker("network", "create", name)
    try:
        syntax = _docker("run", "--rm", *nginx, "-t", check=False)
        assert syntax.returncode == 0, syntax.stderr

        _docker("run", "-d", "--name", name, "-p", "127.0.0.1::80", *nginx, "-g", "daemon off;")
        host, port = _docker("port", name, "80/tcp").stdout.split()[0].rsplit(":", 1)
        deadline = time.monotonic() + 15
        while True:
            try:
                socket.create_connection((host, int(port)), timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline, _docker("logs", name).stderr
                time.sleep(0.2)
        client = httpx.Client(
            base_url=f"http://{host}:{port}", auth=("probe", "probe"), timeout=35, trust_env=False
        )
        with client:
            assert client.get("/health").status_code in {502, 504}
            assert client.get("/wm-api/workers").status_code in {502, 504}

        running = _docker("inspect", "--format", "{{.State.Running}}", name).stdout.strip()
        assert running == "true", _docker("logs", name).stderr
    finally:
        _docker("rm", "-f", name, check=False)
        _docker("network", "rm", name, check=False)
