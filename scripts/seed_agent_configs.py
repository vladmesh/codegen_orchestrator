#!/usr/bin/env python3
"""Seed script for agent configurations.

Populates the database with agent configs from YAML.

Usage:
    python scripts/seed_agent_configs.py [--api-base-url http://localhost:8000]
"""

import argparse
from pathlib import Path
import sys

import httpx
import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "agent_configs.yaml"


def _api_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/api"):
        raise RuntimeError("API_BASE_URL must not include /api")
    return f"{base}/api/{path.lstrip('/')}"


def load_configs(path: Path) -> list[dict]:
    try:
        raw_data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"  Config file not found: {path}")
        return []

    try:
        configs = yaml.safe_load(raw_data)
    except yaml.YAMLError as exc:
        print(f"  Failed to parse YAML: {exc}")
        return []

    if not isinstance(configs, list):
        print(f"  Expected a list of configs in {path.name}")
        return []

    return configs


def seed_agent_configs(api_base_url: str, configs_path: Path) -> bool:
    """Seed agent configurations to the database.

    Returns:
        True if all configs were created successfully
    """
    configs = load_configs(configs_path)
    if not configs:
        return False

    success = True

    with httpx.Client(timeout=30.0) as client:
        for config in configs:
            try:
                resp = client.get(_api_url(api_base_url, f"agent-configs/{config['id']}"))
                if resp.status_code == httpx.codes.OK:
                    payload = {k: v for k, v in config.items() if k != "id"}
                    resp = client.patch(
                        _api_url(api_base_url, f"agent-configs/{config['id']}"),
                        json=payload,
                    )
                    if resp.status_code == httpx.codes.OK:
                        print(f"  Updated agent config: {config['id']}")
                    else:
                        print(
                            f"  Failed to update '{config['id']}': "
                            f"{resp.status_code} - {resp.text}"
                        )
                        success = False
                    continue

                resp = client.post(_api_url(api_base_url, "agent-configs/"), json=config)
                if resp.status_code == httpx.codes.CREATED:
                    print(f"  Created agent config: {config['id']}")
                elif resp.status_code == httpx.codes.CONFLICT:
                    print(f"  Agent config '{config['id']}' already exists")
                else:
                    print(f"  Failed to create '{config['id']}': {resp.status_code} - {resp.text}")
                    success = False

            except httpx.RequestError as e:
                print(f"  Request error for '{config['id']}': {e}")
                success = False

    return success


def main():
    parser = argparse.ArgumentParser(description="Seed agent configurations")
    parser.add_argument(
        "--api-base-url",
        dest="api_base_url",
        default="http://localhost:8000",
        help="API base URL (no /api, default: http://localhost:8000)",
    )
    parser.add_argument(
        "--configs-path",
        default=str(CONFIG_PATH),
        help=f"Path to agent configs YAML (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    print(f"  Seeding configurations to {args.api_base_url}...")

    success = seed_agent_configs(args.api_base_url, Path(args.configs_path))

    if success:
        print("  All configurations seeded successfully!")
        return 0
    else:
        print("  Some configurations failed to seed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
