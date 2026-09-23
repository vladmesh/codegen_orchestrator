"""Compose must not supply DEFAULT_AGENT_TYPE when the deployment did not.

The Python settings have no fallback; a `${DEFAULT_AGENT_TYPE:-claude}` in Compose
would put one back. `:?` makes an unset or blank value stop `docker compose config`.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = "${DEFAULT_AGENT_TYPE:?"


def _services() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]


def test_every_consumer_requires_default_agent_type_from_compose():
    services = _services()

    for name in ("api", "langgraph", "telegram_bot"):
        value = services[name]["environment"]["DEFAULT_AGENT_TYPE"]
        assert value.startswith(REQUIRED), f"{name}: {value!r}"


def test_no_compose_file_supplies_a_default_agent_type_fallback():
    for path in ROOT.glob("docker-compose*.yml"):
        assert "DEFAULT_AGENT_TYPE:-" not in path.read_text(), path.name


def test_env_example_names_an_explicit_default_agent_type():
    lines = (ROOT / ".env.example").read_text().splitlines()

    assert "DEFAULT_AGENT_TYPE=claude" in lines
