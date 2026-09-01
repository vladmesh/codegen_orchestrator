"""Regression coverage for LLM consumers started by test Compose files."""

from pathlib import Path

import yaml

TEST_COMPOSE_ROOT = Path("tests/compose")
ARCHITECT_LLM_ENV = {
    "ARCHITECT_LLM_MODEL",
    "ARCHITECT_LLM_BASE_URL",
    "ARCHITECT_LLM_API_KEY",
}


def _environment_names(service: dict) -> set[str]:
    environment = service.get("environment", [])
    if isinstance(environment, dict):
        return set(environment)
    return {entry.split("=", 1)[0] for entry in environment if isinstance(entry, str)}


def test_architect_consumers_in_test_compose_have_llm_configuration():
    """Architect containers must not exit before their test runner starts."""
    missing_by_service: list[str] = []

    for compose_file in TEST_COMPOSE_ROOT.rglob("*.yml"):
        compose = yaml.safe_load(compose_file.read_text())
        for service_name, service in compose.get("services", {}).items():
            command = " ".join(service.get("command", []))
            if "src.consumers.architect" not in command:
                continue
            missing = ARCHITECT_LLM_ENV - _environment_names(service)
            if missing:
                missing_by_service.append(
                    f"{compose_file}:{service_name} is missing {sorted(missing)}"
                )

    assert not missing_by_service, "\n".join(missing_by_service)
