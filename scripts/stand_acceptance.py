#!/usr/bin/env python3
"""Build and inspect the redacted acceptance artifact for one stand run.

The artifact boundary is deliberately narrow.  The runner may collect only its
known report files; this module copies those files, the original public
lifecycle manifest and one derived final report.  It never receives a checkout,
environment file, key, Docker inspection or provider credential.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

REQUIRED_RUN_FILES = ("junit.xml", "report.tsv", "run.log")
COMBINATION_LOG = re.compile(r"(?:claude|codex)-(?:claude|codex)\.log\Z")
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?:API[_-]?KEY|ACCESS[_-]?TOKEN|OAUTH[_-]?TOKEN|PASSWORD|PRIVATE[_-]?KEY|SECRET)"
    r"\s*(?:=|:)\s*\S+",
    re.IGNORECASE,
)
PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("JSON root must be an object")
    return loaded


def _cost_usd(hourly_cost_cents: object, lifetime_seconds: int) -> str | None:
    if (
        not isinstance(hourly_cost_cents, int)
        or isinstance(hourly_cost_cents, bool)
        or hourly_cost_cents < 0
    ):
        return None
    cost = Decimal(hourly_cost_cents) * Decimal(lifetime_seconds) / Decimal(360000)
    return format(cost.normalize(), "f") if cost else "0"


def _copy_run_outputs(run_dir: Path, output: Path, errors: list[str]) -> None:
    for name in REQUIRED_RUN_FILES:
        source = run_dir / name
        if not source.is_file():
            errors.append(f"required_run_output_missing:{name}")
            continue
        shutil.copyfile(source, output / name)
    if run_dir.is_dir():
        for source in sorted(run_dir.iterdir()):
            if source.is_file() and COMBINATION_LOG.fullmatch(source.name):
                shutil.copyfile(source, output / source.name)


def build_acceptance_artifact(
    manifest_path: Path, run_dir: Path, cleanup_path: Path, output: Path
) -> bool:
    """Build one artifact and return whether its evidence is complete.

    An observation time is the only defensible end of a machine's lifetime.
    Missing timestamps, cleanup proof, or BitLaunch's post-cleanup zero-use
    observation produce an explicitly incomplete report with no invented cost.
    """
    manifest = _load_json(manifest_path)
    try:
        cleanup = _load_json(cleanup_path)
    except (OSError, ValueError, json.JSONDecodeError):
        cleanup = {}
    output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_path, output / "machines.json")
    errors: list[str] = []
    _copy_run_outputs(run_dir, output, errors)

    run_tag = manifest.get("run_tag")
    if not isinstance(run_tag, str) or not run_tag:
        errors.append("manifest_run_tag_unusable")
        run_tag = None
    if cleanup.get("run_tag") != run_tag:
        errors.append("cleanup_run_tag_mismatch")
    cleanup_observed_at = _parse_time(cleanup.get("observed_at"))
    if cleanup_observed_at is None:
        errors.append("cleanup_observation_timestamp_unusable")
    if cleanup.get("status") != "verified":
        errors.append("cleanup_not_verified")
    if cleanup.get("remaining_ids") != []:
        errors.append("run_owned_machines_not_proven_absent")
    if cleanup.get("servers_used") != 0:
        errors.append("post_cleanup_servers_used_not_zero")

    raw_machines = manifest.get("machines")
    if not isinstance(raw_machines, list):
        raw_machines = []
        errors.append("manifest_machines_unusable")
    machine_reports: list[dict[str, object]] = []
    cost_parts: list[Decimal] = []
    cleanup_proven = not {
        "cleanup_observation_timestamp_unusable",
        "cleanup_not_verified",
        "run_owned_machines_not_proven_absent",
        "post_cleanup_servers_used_not_zero",
    }.intersection(errors)
    for machine in raw_machines:
        if not isinstance(machine, dict):
            errors.append("manifest_machine_unusable")
            continue
        created_at = _parse_time(machine.get("created_at"))
        lifetime_seconds: int | None = None
        cost_usd: str | None = None
        if cleanup_proven and created_at is not None and cleanup_observed_at is not None:
            elapsed = (cleanup_observed_at - created_at).total_seconds()
            if elapsed < 0:
                errors.append(f"machine_lifetime_negative:{machine.get('id', 'unknown')}")
            else:
                lifetime_seconds = int(elapsed)
                cost_usd = _cost_usd(machine.get("hourly_cost_cents"), lifetime_seconds)
                if cost_usd is None:
                    errors.append(f"machine_hourly_cost_unusable:{machine.get('id', 'unknown')}")
                else:
                    cost_parts.append(Decimal(cost_usd))
        elif created_at is None:
            errors.append(f"machine_creation_timestamp_unusable:{machine.get('id', 'unknown')}")
        machine_reports.append(
            {
                "id": machine.get("id"),
                "role": machine.get("role"),
                "created_at": machine.get("created_at"),
                "cleanup_observed_at": cleanup.get("observed_at"),
                "lifetime_seconds": lifetime_seconds,
                "hourly_cost_cents": machine.get("hourly_cost_cents"),
                "cost_usd": cost_usd,
            }
        )
    if len(machine_reports) != len(raw_machines) or not machine_reports:
        errors.append("machine_evidence_incomplete")
    if any(machine["cost_usd"] is None for machine in machine_reports):
        errors.append("run_cost_incomplete")
    total_cost = (
        sum(cost_parts, Decimal("0"))
        if machine_reports and len(cost_parts) == len(machine_reports)
        else None
    )
    report = {
        "schema_version": "v1",
        "status": "complete" if not errors else "incomplete",
        "run_tag": run_tag,
        "cleanup": cleanup,
        "machines": machine_reports,
        "run_cost_usd": format(total_cost.normalize(), "f") if total_cost is not None else None,
        "incompleteness": sorted(set(errors)),
    }
    (output / "final-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return not errors


def scan_artifact(artifact: Path, *, canaries: tuple[str, ...] = ()) -> list[str]:
    """Return value-free redaction failures for the fixed artifact allow-list."""
    errors: list[str] = []
    allowed = {"machines.json", "final-report.json", *REQUIRED_RUN_FILES}
    for path in sorted(artifact.iterdir()) if artifact.is_dir() else []:
        if path.name not in allowed and not COMBINATION_LOG.fullmatch(path.name):
            errors.append("candidate contains an unapproved file")
            continue
        if not path.is_file():
            errors.append("candidate contains a non-file entry")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(canary and canary in text for canary in canaries):
            errors.append("candidate contains a supplied redaction canary")
        if SENSITIVE_ASSIGNMENT.search(text) or PRIVATE_KEY_MARKER.search(text):
            errors.append("candidate contains credential-shaped text")
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--manifest", required=True, type=Path)
    build.add_argument("--run-dir", required=True, type=Path)
    build.add_argument("--cleanup", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    scan = sub.add_parser("scan")
    scan.add_argument("--artifact", required=True, type=Path)
    scan.add_argument(
        "--canary-env",
        action="append",
        default=[],
        help="environment variable whose supplied test value must not be present",
    )
    args = parser.parse_args()
    if args.command == "build":
        try:
            complete = build_acceptance_artifact(
                args.manifest, args.run_dir, args.cleanup, args.output
            )
        except (OSError, ValueError, json.JSONDecodeError, InvalidOperation):
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output / "final-report.json").write_text(
                json.dumps(
                    {
                        "schema_version": "v1",
                        "status": "incomplete",
                        "incompleteness": ["builder_input_unusable"],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            return 2
        return 0 if complete else 2
    canaries = tuple(
        value for name in args.canary_env if (value := os.environ.get(name)) is not None
    )
    return 0 if not scan_artifact(args.artifact, canaries=canaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
