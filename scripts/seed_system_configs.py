#!/usr/bin/env python3
"""Seed script for system configurations.

Writes the operational constants declared in system_configs.yaml to the database.
Ordinary keys are reconciled to their file values. Paid-work controls are
different: the typed initialize-only API inserts their defaults only when absent
and preserves every existing operator value. Runs on every deploy, right after
migrations.

Usage:
    python scripts/seed_system_configs.py [--api-base-url http://localhost:8000]
"""

import argparse
from pathlib import Path
import sys

import httpx
import yaml

from shared.clients.internal_api import InternalAPISyncClient

CONFIG_PATH = Path(__file__).resolve().parent / "system_configs.yaml"
_WORK_ADMISSION_KEYS = {
    "work_admission.emergency_stop",
    "work_admission.max_projects_per_user",
    "work_admission.max_concurrent_paid_runs",
    "work_admission.engineering_executor_override",
    "work_admission.qa_executor_override",
}
# Keys the file seeds once and then leaves to the operator. An operator raises
# engineering slots on a running installation after watching it; a deploy that
# reset the value back to the file's would take capacity away silently, which is
# exactly the kind of quiet divergence this contour is trying to stop.
_OPERATOR_OWNED_KEYS = frozenset({"engineering.worker_slots"})
_PAID_WORK_CONTROL_FIELDS = {
    "work_admission.emergency_stop": "emergency_stop",
    "work_admission.max_concurrent_paid_runs": "max_concurrent_paid_runs",
    "work_admission.engineering_executor_override": "engineering_executor_override",
    "work_admission.qa_executor_override": "qa_executor_override",
}


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


def _current_value(client: InternalAPISyncClient, key: str) -> tuple[bool, object]:
    """Return (exists, value) for a key already in the database."""
    resp = client.get_raw(f"system-configs/{key}")
    if resp.status_code == httpx.codes.NOT_FOUND:
        return False, None
    if resp.status_code != httpx.codes.OK:
        raise RuntimeError(f"HTTP {resp.status_code} reading '{key}': {resp.text}")
    return True, resp.json()["value"]


def seed_system_configs(
    api_base_url: str, configs_path: Path, *, skip_keys: frozenset[str] = frozenset()
) -> bool:
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
    paid_work_controls: dict[str, object] = {}

    # Seeding is an internal call like any other, so it goes through the transport
    # that carries X-Internal-Key: every route under /api requires a caller.
    client = InternalAPISyncClient(api_base_url)
    try:
        for config in configs:
            key = config["key"]
            if key in skip_keys:
                continue
            value = config["value"]

            field = _PAID_WORK_CONTROL_FIELDS.get(key)
            if field is not None:
                paid_work_controls[field] = value
                continue

            try:
                exists, db_value = _current_value(client, key)
                if exists and db_value == value:
                    unchanged += 1
                    continue

                if exists and key in _OPERATOR_OWNED_KEYS:
                    print(f"  Kept operator-owned '{key}': db={db_value!r} (file={value!r})")
                    unchanged += 1
                    continue

                payload = {
                    "key": key,
                    "value": value,
                    "category": config["category"],
                    "description": config["description"],
                    "updated_by": "seed",
                }
                if key in _WORK_ADMISSION_KEYS:
                    resp = client.request_raw(
                        "PUT", f"work-admission/controls/{key}", json={"value": value}
                    )
                    expected_status = httpx.codes.OK
                else:
                    resp = client.request_raw("POST", "system-configs/", json=payload)
                    expected_status = httpx.codes.CREATED
                if resp.status_code != expected_status:
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

        if paid_work_controls:
            missing_fields = set(_PAID_WORK_CONTROL_FIELDS.values()) - paid_work_controls.keys()
            if missing_fields:
                print(
                    "  Paid-work controls are incomplete in the seed file: "
                    + ", ".join(sorted(missing_fields))
                )
                success = False
            else:
                try:
                    resp = client.request_raw(
                        "POST", "work-admission/controls/initialize", json=paid_work_controls
                    )
                    if resp.status_code != httpx.codes.OK:
                        print(
                            "  Failed to initialize paid-work controls: "
                            f"{resp.status_code} - {resp.text}"
                        )
                        success = False
                except httpx.RequestError as e:
                    print(f"  Request error for paid-work controls: {e}")
                    success = False
    finally:
        client.close()

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
    parser.add_argument(
        "--skip-key",
        action="append",
        default=[],
        help="Key to leave untouched; repeat for each test-only overlay key",
    )
    args = parser.parse_args()

    print(f"  Seeding system configurations to {args.api_base_url}...")

    success = seed_system_configs(
        args.api_base_url, Path(args.configs_path), skip_keys=frozenset(args.skip_key)
    )

    if success:
        print("  All system configurations seeded successfully!")
        return 0
    else:
        print("  Some system configurations failed to seed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
