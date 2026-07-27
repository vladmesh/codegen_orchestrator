#!/usr/bin/env python3
"""Seed script for system configurations.

Writes the operational constants declared in system_configs.yaml to the database.
The file wins: every key it declares is written with its file value, overwriting
whatever is in the DB. A key that the file does not declare is left untouched.
Runs on every deploy, right after migrations, so a value edited in the file
reaches the system without a manual step.

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


def _current_value(client: httpx.Client, api_base_url: str, key: str) -> tuple[bool, object]:
    """Return (exists, value) for a key already in the database."""
    resp = client.get(_api_url(api_base_url, f"system-configs/{key}"))
    if resp.status_code == httpx.codes.NOT_FOUND:
        return False, None
    if resp.status_code != httpx.codes.OK:
        raise RuntimeError(f"HTTP {resp.status_code} reading '{key}': {resp.text}")
    return True, resp.json()["value"]


def seed_system_configs(api_base_url: str, configs_path: Path) -> bool:
    """Write every config declared in the YAML file to the database.

    Existing values are overwritten. Keys that diverged from the file are
    printed with both values, so a drift is never corrected silently.

    Returns:
        True if all configs were processed successfully
    """
    configs = load_configs(configs_path)
    if not configs:
        return False

    success = True
    created = 0
    updated = 0
    unchanged = 0

    with httpx.Client(timeout=30.0) as client:
        for config in configs:
            key = config["key"]
            value = config["value"]

            try:
                exists, db_value = _current_value(client, api_base_url, key)
                if exists and db_value == value:
                    unchanged += 1
                    continue

                payload = {
                    "key": key,
                    "value": value,
                    "category": config["category"],
                    "description": config["description"],
                    "updated_by": "seed",
                }
                resp = client.post(
                    _api_url(api_base_url, "system-configs/"),
                    json=payload,
                )
                if resp.status_code != httpx.codes.CREATED:
                    print(f"  Failed to write '{key}': {resp.status_code} - {resp.text}")
                    success = False
                    continue

                if exists:
                    print(f"  Overwrote '{key}': db={db_value!r} -> file={value!r}")
                    updated += 1
                else:
                    created += 1

            except (httpx.RequestError, RuntimeError) as e:
                print(f"  Request error for '{key}': {e}")
                success = False

    print(
        f"  System configs: {created} created, {updated} overwritten, {unchanged} already in sync"
    )
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
