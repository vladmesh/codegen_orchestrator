"""Loki carries the registry's expected misses at info and its real failures at error.

Distribution v2.8.3 logs every response that carries a registry error code at error level
(`App.logError`, registry/handlers/app.go). Its json formatter (`configureLogging`,
registry/registry.go) is logrus's JSONFormatter, which writes the code as `err.code` in the
spelling of `ErrorCode.Error()` (registry/api/errcode/errors.go: "manifest unknown") beside
the response fields of `GetResponseLogger` (context/http.go: `http.response.status`).
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from tests.unit.test_production_compose_mounts import PROD, _render

ROOT = Path(__file__).parents[2]
PROMTAIL = ROOT / "infra/promtail.yml"
#: A `--dry-run` entry: timestamp, label set, line. The columns go through a tabwriter that
#: pads with tabs, so a cell whose width is a multiple of 8 is followed by no separator.
ENTRY = re.compile(r"^[^\s{]+\s*(\{[^{}]*\})\s*(\{.*\})$")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


@pytest.mark.docker
def test_the_production_registry_logs_json(tmp_path):
    registry = _render(PROD, tmp_path)["services"]["registry"]

    assert registry["environment"]["REGISTRY_LOG_FORMATTER"] == "json"


def _registry_error(request_id: str, method: str, uri: str, status: int, code: str, message: str):
    """One `response completed with error` line as distribution's JSONFormatter writes it."""
    return {
        "err.code": code,
        "err.message": message,
        "go.version": "go1.20.8",
        "http.request.host": "registry:5000",
        "http.request.id": request_id,
        "http.request.method": method,
        "http.request.remoteaddr": "172.20.0.7:51234",
        "http.request.uri": uri,
        "http.request.useragent": "docker/27.3.1 go/go1.22.7",
        "http.response.contenttype": "application/json; charset=utf-8",
        "http.response.duration": "1.92ms",
        "http.response.status": status,
        "http.response.written": 96,
        "instance.id": "5c0a3d7e-6a4f-4c0e-9a31-2f4f5d8e0b11",
        "level": "error",
        "msg": "response completed with error",
        "service": "registry",
        "time": "2026-01-02T03:04:05.123456789Z",
        "vars.name": "proj/app",
        "version": "v2.8.3",
    }


def _promtail(tmp_path: Path, compose_service: str, entries: list[dict], key: str) -> dict:
    """The labels the pipeline of infra/promtail.yml gives each entry, by `entry[key]`."""
    (scrape,) = yaml.safe_load(PROMTAIL.read_text())["scrape_configs"]
    config = tmp_path / f"promtail-{compose_service}.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "server": {"http_listen_port": 0, "grpc_listen_port": 0},
                "clients": [{"url": "http://127.0.0.1:3100/loki/api/v1/push"}],
                "scrape_configs": [
                    {
                        "job_name": scrape["job_name"],
                        "static_configs": [
                            {
                                "targets": ["localhost"],
                                "labels": {"compose_service": compose_service},
                            }
                        ],
                        "pipeline_stages": scrape["pipeline_stages"],
                    }
                ],
            }
        )
    )
    wrapped = [
        json.dumps(
            {
                "log": json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n",
                "stream": "stderr",
                "time": "2026-01-02T03:04:05.1Z",
            }
        )
        for entry in entries
    ]
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    image = compose["services"]["promtail"]["image"]
    docker = shutil.which("docker")
    assert docker is not None
    subprocess.run([docker, "pull", image], check=True, capture_output=True, timeout=60)
    result = subprocess.run(
        [
            *(docker, "run", "--rm", "-i", "--network", "none"),
            *("-v", f"{config}:/etc/promtail/probe.yml:ro", image),
            *("-config.file=/etc/promtail/probe.yml", "--stdin", "--dry-run"),
        ],
        input="\n".join(wrapped) + "\n",
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    labels = {}
    for line in result.stdout.splitlines():
        if match := ENTRY.match(line):
            entry = json.loads(match[2])
            labels[entry[key]] = dict(LABEL.findall(match[1]))
    assert len(labels) == len(entries), result.stdout + result.stderr
    return labels


@pytest.mark.docker
@pytest.mark.timeout(240)
def test_promtail_ships_registry_misses_as_info_and_keeps_every_other_level(tmp_path):
    registry = [
        _registry_error(
            "manifest-get-miss",
            "GET",
            "/v2/proj/app/manifests/v1",
            404,
            "manifest unknown",
            "manifest unknown",
        ),
        _registry_error(
            "manifest-head-miss",
            "HEAD",
            "/v2/proj/app/manifests/v1",
            404,
            "manifest unknown",
            "manifest unknown",
        ),
        _registry_error(
            "blob-head-miss",
            "HEAD",
            "/v2/proj/app/blobs/sha256:" + "a" * 64,
            404,
            "blob unknown",
            "blob unknown to registry",
        ),
        _registry_error(
            "repository-miss",
            "GET",
            "/v2/proj/new/tags/list",
            404,
            "name unknown",
            "repository name not known to registry",
        ),
        _registry_error(
            "storage-failure", "GET", "/v2/proj/app/manifests/v1", 500, "unknown", "unknown error"
        ),
        _registry_error(
            "auth-refused",
            "GET",
            "/v2/proj/app/manifests/v1",
            401,
            "unauthorized",
            "authentication required",
        ),
        _registry_error(
            "push-denied",
            "PUT",
            "/v2/proj/app/manifests/v1",
            403,
            "denied",
            "requested access to the resource is denied",
        ),
        _registry_error(
            "manifest-references-missing-blob",
            "PUT",
            "/v2/proj/app/manifests/v1",
            400,
            "manifest blob unknown",
            "blob unknown to registry",
        ),
    ]
    services = [
        {"timestamp": "2026-01-02T03:04:05Z", "level": "warn", "service": "api", "event": "slow"},
        {
            "timestamp": "2026-01-02T03:04:05Z",
            "level": "error",
            "service": "api",
            "event": "failed",
        },
    ]

    registry_levels = {
        key: labels["level"]
        for key, labels in _promtail(tmp_path, "registry", registry, "http.request.id").items()
    }
    service_levels = {
        key: labels["level"]
        for key, labels in _promtail(tmp_path, "api", services, "event").items()
    }

    assert registry_levels == {
        "manifest-get-miss": "info",
        "manifest-head-miss": "info",
        "blob-head-miss": "info",
        "repository-miss": "info",
        "storage-failure": "error",
        "auth-refused": "error",
        "push-denied": "error",
        "manifest-references-missing-blob": "error",
    }
    assert service_levels == {"slow": "warning", "failed": "error"}
