"""Compose refuses to start a registry-secrets writer without its lease policy or receipt key.

Each writer's settings have no fallback; `:?` makes an unset or blank value stop
`docker compose config` before any of them starts.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WRITERS = ("scheduler-pipeline", "engineering-worker", "scaffolder", "deploy-worker")


def test_every_registry_writer_requires_the_lease_policy_and_the_receipt_key():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]

    for name in WRITERS:
        environment = services[name]["environment"]
        for variable in ("GITHUB_REGISTRY_SECRETS_LEASE_SECONDS", "SECRETS_ENCRYPTION_KEY"):
            assert environment[variable].startswith(f"${{{variable}:?"), (name, variable)
