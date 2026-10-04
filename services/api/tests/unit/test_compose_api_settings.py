"""Test API containers carry their required connectivity without host defaults."""

from pathlib import Path

from pydantic import ValidationError
import pytest
import yaml

from src.config import Settings

ROOT = Path(__file__).resolve().parents[4]


def _api_containers():
    paths = [ROOT / "docker-compose.yml", *sorted((ROOT / "tests/compose").rglob("*.yml"))]
    for path in paths:
        for name, service in yaml.safe_load(path.read_text())["services"].items():
            if service.get("build", {}).get(
                "dockerfile"
            ) == "services/api/Dockerfile" or service.get("image", "").startswith(
                "codegen-orchestrator/api:"
            ):
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
    # The production base has Compose interpolation for other required values;
    # only connectivity is literal. Synthetic fixtures validate independently.
    if path != ROOT / "docker-compose.yml":
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        assert Settings(_env_file=None).worker_manager_url == environment["WORKER_MANAGER_URL"]


def test_missing_worker_manager_configuration_still_fails_startup(monkeypatch):
    monkeypatch.delenv("WORKER_MANAGER_URL", raising=False)
    monkeypatch.setenv("LK_JWT_SECRET", "synthetic")
    with pytest.raises(ValidationError, match="worker_manager_url"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://test:test@localhost/test",
            redis_url="redis://localhost:6379/0",
            internal_api_key="synthetic",
            default_agent_type="claude",
        )


@pytest.mark.parametrize("overlay", ["docker-compose.prod.yml", "docker-compose.stand.yml"])
def test_production_and_stand_inherit_the_explicit_base_manager_endpoint(overlay):
    class ComposeLoader(yaml.SafeLoader):
        pass

    def compose_value(loader, node):
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return loader.construct_scalar(node)

    ComposeLoader.add_constructor("!reset", compose_value)
    ComposeLoader.add_constructor("!override", compose_value)
    loader = ComposeLoader((ROOT / overlay).read_text())
    try:
        api = loader.get_single_data()["services"]["api"]
    finally:
        loader.dispose()
    # These overlays deliberately change runtime isolation and health checks,
    # inheriting the whole base API environment rather than redefining it.
    assert "environment" not in api
    base = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["api"]
    assert base["environment"]["WORKER_MANAGER_URL"] == "http://worker-manager:8000"
