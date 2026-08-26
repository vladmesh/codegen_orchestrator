"""Production admin entry remains a loopback-only, Basic-Auth-protected surface."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).parents[2]


def _production_compose() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        project_dir = Path(tmp)
        env_file = project_dir / ".env"
        shutil.copy(ROOT / ".env.example", env_file)
        env_file.write_text(env_file.read_text() + "\nLOKI_URL=http://loki:3100\n")
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(project_dir),
                "--env-file",
                str(env_file),
                "-f",
                str(ROOT / "docker-compose.yml"),
                "-f",
                str(ROOT / "docker-compose.prod.yml"),
                "config",
                "--format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    return json.loads(result.stdout)


def test_production_admin_is_exposed_only_on_host_loopback():
    config = _production_compose()
    assert config["services"]["admin-frontend"]["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 80,
            "published": "3001",
            "protocol": "tcp",
        }
    ]


def test_admin_is_not_routed_by_public_caddy_and_nginx_keeps_all_surfaces_authenticated():
    caddy = (ROOT / "infra/Caddyfile").read_text()
    nginx = (ROOT / "services/admin-frontend/nginx.conf").read_text()

    assert "admin-frontend" not in caddy
    assert all(path not in caddy for path in ("/api/", "/wm-api/", "/grafana/"))
    assert 'auth_basic "Orchestrator Admin";' in nginx
    for location in (
        "location / {",
        "location /api/ {",
        "location /wm-api/ {",
        "location /grafana/ {",
    ):
        assert location in nginx
    assert "location = /health {\n        auth_basic off;" in nginx


def test_admin_credentials_have_no_default_value():
    compose = (ROOT / "docker-compose.yml").read_text()
    entrypoint = (ROOT / "services/admin-frontend/entrypoint.sh").read_text()

    assert "ADMIN_USER: ${ADMIN_USER}" in compose
    assert "ADMIN_USER:-" not in entrypoint
    assert 'if [ -z "$ADMIN_USER" ]; then' in entrypoint


def test_deploy_guide_records_the_supported_tunnel_and_safety_checks():
    deploy = (ROOT / "docs/DEPLOY.md").read_text()

    assert "ssh -N -L 3001:127.0.0.1:3001 deploy@PROD_HOST" in deploy
    assert "http://127.0.0.1:3001" in deploy
    assert "sudo ss -ltn '( sport = :3001 )'" in deploy
    assert (
        "rg -n 'admin-frontend|handle /api|handle /wm-api|handle /grafana' infra/Caddyfile"
        in deploy
    )
