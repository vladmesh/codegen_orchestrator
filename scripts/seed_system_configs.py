#!/usr/bin/env python3
"""Seed script for system configurations.

Populates the database with default operational constants from YAML.
Uses upsert semantics — existing keys are NOT overwritten (preserves admin edits).

Usage:
    python scripts/seed_system_configs.py [--api-base-url http://localhost:8000]
"""

import argparse
from pathlib import Path
import sys

import httpx
import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "system_configs.yaml"


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


def seed_system_configs(api_base_url: str, configs_path: Path) -> bool:
    """Seed system configurations to the database.

    Only creates new keys — does NOT overwrite existing values
    (so admin edits via UI are preserved).

    Returns:
        True if all configs were processed successfully
    """
    configs = load_configs(configs_path)
    if not configs:
        return False

    success = True
    created = 0
    skipped = 0

    with httpx.Client(timeout=30.0) as client:
        for config in configs:
            key = config.get("key")
            if not key:
                print("  Skipping config without key")
                continue

            try:
                # Check if key exists
                resp = client.get(_api_url(api_base_url, f"system-configs/{key}"))
                if resp.status_code == httpx.codes.OK:
                    skipped += 1
                    continue

                # Create new config
                payload = {
                    "key": key,
                    "value": config["value"],
                    "category": config.get("category", "uncategorized"),
                    "description": config.get("description"),
                    "updated_by": "seed",
                }
                resp = client.post(
                    _api_url(api_base_url, "system-configs/"),
                    json=payload,
                )
                if resp.status_code == httpx.codes.CREATED:
                    created += 1
                else:
                    print(f"  Failed to create '{key}': {resp.status_code} - {resp.text}")
                    success = False

            except httpx.RequestError as e:
                print(f"  Request error for '{key}': {e}")
                success = False

    print(f"  System configs: {created} created, {skipped} already exist")
    return success


def main():
    parser = argparse.ArgumentParser(description="Seed system configurations")
    parser.add_argument(
        "--api-base-url",
        dest="api_base_url",
        default="http://localhost:8000",
        help="API base URL (no /api, default: http://localhost:8000)",
    )
    parser.add_argument(
        "--configs-path",
        default=str(CONFIG_PATH),
        help=f"Path to system configs YAML (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    print(f"  Seeding system configurations to {args.api_base_url}...")

    success = seed_system_configs(args.api_base_url, Path(args.configs_path))

    if success:
        print("  All system configurations seeded successfully!")
        return 0
    else:
        print("  Some system configurations failed to seed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
