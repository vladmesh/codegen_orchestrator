"""Worker-manager compose surfaces must satisfy fail-fast connectivity config."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    Path("tests/compose/service/worker-manager.yml"),
    Path("tests/compose/integration/backend-dind.yml"),
)
REQUIRED_CONNECTIVITY = {
    "REDIS_URL",
    "API_BASE_URL",
    "WORKER_BROKER_URL",
    "WORKER_BROKER_INTERNAL_TOKEN",
}


def _environment_keys(environment: list[str] | dict[str, object]) -> set[str]:
    if isinstance(environment, dict):
        return set(environment)
    return {entry.split("=", 1)[0] for entry in environment}


@pytest.mark.parametrize("relative_path", COMPOSE_FILES, ids=str)
def test_worker_manager_compose_sets_required_connectivity(relative_path: Path) -> None:
    compose = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    environment = compose["services"]["worker-manager"]["environment"]

    missing = REQUIRED_CONNECTIVITY - _environment_keys(environment)

    assert missing == set(), f"{relative_path}: missing worker-manager settings {sorted(missing)}"
