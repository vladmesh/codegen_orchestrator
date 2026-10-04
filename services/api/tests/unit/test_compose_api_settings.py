"""Test API containers carry their required connectivity without host defaults."""

from pathlib import Path

import pytest
import yaml

from src.config import Settings

ROOT = Path(__file__).resolve().parents[4]


def _api_containers():
    for path in sorted((ROOT / "tests/compose").rglob("*.yml")):
        for name, service in yaml.safe_load(path.read_text())["services"].items():
            if service.get("build", {}).get("dockerfile") == "services/api/Dockerfile":
                yield pytest.param(path, name, service, id=f"{path.relative_to(ROOT)}:{name}")


@pytest.mark.parametrize("path,name,service", list(_api_containers()))
def test_api_compose_provides_required_worker_manager_connectivity(
    path, name, service, monkeypatch
):
    environment = service["environment"]
    if isinstance(environment, list):
        environment = dict(entry.split("=", 1) for entry in environment)
    assert environment.get("WORKER_MANAGER_URL") == "http://worker-manager:8000", (
        f"{path}:{name} must provide the required publication execution endpoint"
    )
    for key in (
        "DATABASE_URL",
        "REDIS_URL",
        "WORKER_MANAGER_URL",
        "LK_JWT_SECRET",
        "INTERNAL_API_KEY",
        "DEFAULT_AGENT_TYPE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    assert Settings(_env_file=None).worker_manager_url == environment["WORKER_MANAGER_URL"]
