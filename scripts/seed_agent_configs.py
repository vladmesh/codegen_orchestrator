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


def load_agent_configs(configs_path: Path) -> list[dict]:
    try:
        raw_data = configs_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"❌ Config file not found: {configs_path}")
        return []

    try:
        configs = yaml.safe_load(raw_data)
    except yaml.YAMLError as exc:
        print(f"❌ Failed to parse YAML: {exc}")
        return []

    if not isinstance(configs, list):
        print("❌ Expected a list of agent configs in YAML")
        return []

    return configs


def seed_agent_configs(api_url: str, configs_path: Path) -> bool:
    """Seed agent configurations to the database.

    Args:
        api_url: Base URL of the API service

    Returns:
        True if all configs were created successfully
    """
    configs = load_agent_configs(configs_path)
    if not configs:
        return False

    success = True

    with httpx.Client(timeout=30.0) as client:
        for config in configs:
            try:
                # Check if already exists
                resp = client.get(f"{api_url}/api/agent-configs/{config['id']}")
                if resp.status_code == httpx.codes.OK:
                    print(f"  ⏭️  Agent config '{config['id']}' already exists, skipping")
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
    args = parser.parse_args()

    print(f"🌱 Seeding agent configurations to {args.api_url}...")

    if seed_agent_configs(args.api_url, Path(args.configs_path)):
        print("✅ Agent configs seeded successfully!")
        return 0
    else:
        print("⚠️  Some agent configs failed to seed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
