#!/usr/bin/env python3
"""Seed script for agent configurations.

This script populates the database with agent prompts extracted from the
original hardcoded values in the LangGraph node files.

Usage:
    python scripts/seed_agent_configs.py [--api-url http://localhost:8000]
"""

import argparse
from pathlib import Path
import sys

import httpx
import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "agent_configs.yaml"
CLI_CONFIG_PATH = Path(__file__).resolve().parent / "cli_agent_configs.yaml"


def load_configs(path: Path) -> list[dict]:
    try:
        raw_data = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"❌ Config file not found: {path}")
        return []

    try:
        configs = yaml.safe_load(raw_data)
    except yaml.YAMLError as exc:
        print(f"❌ Failed to parse YAML: {exc}")
        return []

    if not isinstance(configs, list):
        print(f"❌ Expected a list of configs in {path.name}")
        return []

    return configs


def seed_agent_configs(api_url: str, configs_path: Path) -> bool:
    """Seed agent configurations to the database.

    Args:
        api_url: Base URL of the API service

    Returns:
        True if all configs were created successfully
    """
    configs = load_configs(configs_path)
    if not configs:
        return False

    success = True
    print("\n📦 Seeding LLM Agents:")

    with httpx.Client(timeout=30.0) as client:
        for config in configs:
            try:
                # Check if already exists
                resp = client.get(f"{api_url}/api/agent-configs/{config['id']}")
                if resp.status_code == httpx.codes.OK:
                    payload = {k: v for k, v in config.items() if k != "id"}
                    resp = client.patch(
                        f"{api_url}/api/agent-configs/{config['id']}",
                        json=payload,
                    )
                    if resp.status_code == httpx.codes.OK:
                        print(f"  🔁 Updated agent config: {config['id']}")
                    else:
                        print(
                            f"  ❌ Failed to update '{config['id']}': "
                            f"{resp.status_code} - {resp.text}"
                        )
                        success = False
                    continue

                # Create new config
                resp = client.post(f"{api_url}/api/agent-configs/", json=config)
                if resp.status_code == httpx.codes.CREATED:
                    print(f"  ✅ Created agent config: {config['id']}")
                elif resp.status_code == httpx.codes.CONFLICT:
                    print(f"  ⏭️  Agent config '{config['id']}' already exists")
                else:
                    print(
                        f"  ❌ Failed to create '{config['id']}': {resp.status_code} - {resp.text}"
                    )
                    success = False

            except httpx.RequestError as e:
                print(f"  ❌ Request error for '{config['id']}': {e}")
                success = False

    return success


def seed_cli_agent_configs(api_url: str, configs_path: Path) -> bool:
    """Seed CLI agent configurations to the database."""
    configs = load_configs(configs_path)
    if not configs:
        return False

    success = True
    print("\n🛠️  Seeding CLI Agents:")

    with httpx.Client(timeout=30.0) as client:
        for config in configs:
            try:
                # Check if already exists
                resp = client.get(f"{api_url}/api/cli-agent-configs/{config['id']}")
                if resp.status_code == httpx.codes.OK:
                    payload = {k: v for k, v in config.items() if k != "id"}
                    resp = client.patch(
                        f"{api_url}/api/cli-agent-configs/{config['id']}",
                        json=payload,
                    )
                    if resp.status_code == httpx.codes.OK:
                        print(f"  🔁 Updated CLI agent config: {config['id']}")
                    else:
                        print(
                            f"  ❌ Failed to update CLI '{config['id']}': "
                            f"{resp.status_code} - {resp.text}"
                        )
                        success = False
                    continue

                # Create new config
                resp = client.post(f"{api_url}/api/cli-agent-configs/", json=config)
                if resp.status_code == httpx.codes.CREATED:
                    print(f"  ✅ Created CLI agent config: {config['id']}")
                elif resp.status_code == httpx.codes.CONFLICT:
                    print(f"  ⏭️  CLI Agent config '{config['id']}' already exists")
                else:
                    print(
                        f"  ❌ Failed to create '{config['id']}': {resp.status_code} - {resp.text}"
                    )
                    success = False

            except httpx.RequestError as e:
                print(f"  ❌ Request error for '{config['id']}': {e}")
                success = False

    return success


def main():
    parser = argparse.ArgumentParser(description="Seed agent configurations")
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--configs-path",
        default=str(CONFIG_PATH),
        help=f"Path to agent configs YAML (default: {CONFIG_PATH})",
    )
    parser.add_argument(
        "--cli-configs-path",
        default=str(CLI_CONFIG_PATH),
        help=f"Path to CLI agent configs YAML (default: {CLI_CONFIG_PATH})",
    )
    args = parser.parse_args()

    print(f"🌱 Seeding configurations to {args.api_url}...")

    llm_success = seed_agent_configs(args.api_url, Path(args.configs_path))
    cli_success = seed_cli_agent_configs(args.api_url, Path(args.cli_configs_path))

    if llm_success and cli_success:
        print("\n✅ All configurations seeded successfully!")
        return 0
    else:
        print("\n⚠️  Some configurations failed to seed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
