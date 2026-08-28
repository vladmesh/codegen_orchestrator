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
REMOTE_INVOCATION_LOG = "remote-invocation.log"
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


def _milliusd_to_usd(raw: int) -> str:
    cost = Decimal(raw) / Decimal(1000)
    return format(cost.normalize(), "f") if cost else "0"


def _estimated_cost(
    rate_milliusd_per_hour: object, lifetime_seconds: int
) -> dict[str, object] | None:
    """Return the documented per-server rate estimate, never an account rate."""
    if (
        not isinstance(rate_milliusd_per_hour, int)
        or isinstance(rate_milliusd_per_hour, bool)
        or rate_milliusd_per_hour < 0
    ):
        return None
    billed_hours = max(1, (lifetime_seconds + 3599) // 3600)
    return {
        "status": "estimated",
        "source": "per_server_rate",
        "unit": "USD*1000 per hour",
        "billing_rounding": "hours rounded up",
        "billed_hours": billed_hours,
        "usd": _milliusd_to_usd(rate_milliusd_per_hour * billed_hours),
    }


def _incomplete_cost() -> dict[str, object]:
    return {"status": "incomplete", "source": None, "unit": None, "usd": None}


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
        source = run_dir / REMOTE_INVOCATION_LOG
        if source.is_file():
            shutil.copyfile(source, output / source.name)


def _cleanup_proof(
    cleanup: dict[str, Any], run_tag: str | None, errors: list[str]
) -> datetime | None:
    if cleanup.get("run_tag") != run_tag:
        errors.append("cleanup_run_tag_mismatch")
    observed_at = _parse_time(cleanup.get("observed_at"))
    if observed_at is None:
        errors.append("cleanup_observation_timestamp_unusable")
    if cleanup.get("status") != "verified":
        errors.append("cleanup_not_verified")
    if cleanup.get("remaining_ids") != []:
        errors.append("run_owned_machines_not_proven_absent")
    if cleanup.get("servers_used") != 0:
        errors.append("post_cleanup_servers_used_not_zero")
    return observed_at


def _usage_by_machine(
    cleanup: dict[str, Any], errors: list[str]
) -> tuple[bool, dict[object, dict[str, object]]]:
    usage = cleanup.get("usage")
    status = usage.get("status") if isinstance(usage, dict) else "absent"
    rows = usage.get("observations") if isinstance(usage, dict) else []
    if status == "uncorrelated":
        errors.append("run_owned_usage_uncorrelated")
        return False, {}
    if status != "observed":
        return True, {}
    if not isinstance(rows, list):
        errors.append("run_owned_usage_uncorrelated")
        return False, {}
    observations: dict[object, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict):
            errors.append("run_owned_usage_uncorrelated")
            continue
        machine_id, cost, hours = row.get("machine_id"), row.get("cost_milliusd"), row.get("hours")
        if (
            machine_id in observations
            or not isinstance(cost, int)
            or isinstance(cost, bool)
            or cost < 0
            or not isinstance(hours, int)
            or isinstance(hours, bool)
            or hours < 1
        ):
            errors.append("run_owned_usage_uncorrelated")
            continue
        observations[machine_id] = row
    return not errors or "run_owned_usage_uncorrelated" not in errors, observations


def _machine_report(
    machine: dict[str, object],
    cleanup: dict[str, Any],
    cleanup_observed_at: datetime | None,
    cleanup_proven: bool,
    usage_reliable: bool,
    usage: dict[object, dict[str, object]],
    errors: list[str],
) -> tuple[dict[str, object], Decimal | None, Decimal | None]:
    machine_id = machine.get("id", "unknown")
    created_at = _parse_time(machine.get("created_at"))
    lifetime_seconds: int | None = None
    cost = _incomplete_cost()
    estimate_part: Decimal | None = None
    actual_part: Decimal | None = None
    if cleanup_proven and created_at is not None and cleanup_observed_at is not None:
        elapsed = (cleanup_observed_at - created_at).total_seconds()
        if elapsed < 0:
            errors.append(f"machine_lifetime_negative:{machine_id}")
        else:
            lifetime_seconds = int(elapsed)
            estimate = _estimated_cost(machine.get("rate_milliusd_per_hour"), lifetime_seconds)
            if estimate is None or machine.get("rate_unit") != "USD*1000 per hour":
                errors.append(f"machine_rate_unusable:{machine_id}")
            elif usage_reliable:
                estimate_part = Decimal(str(estimate["usd"]))
                observation = usage.get(machine.get("id"))
                if observation is None:
                    cost = estimate
                else:
                    raw_cost = observation["cost_milliusd"]
                    assert isinstance(raw_cost, int)
                    cost = {
                        "status": "actual",
                        "source": "provider_usage",
                        "unit": "USD*1000",
                        "billed_hours": observation["hours"],
                        "usd": _milliusd_to_usd(raw_cost),
                    }
                    actual_part = Decimal(str(cost["usd"]))
    elif created_at is None:
        errors.append(f"machine_creation_timestamp_unusable:{machine_id}")
    return (
        {
            "id": machine.get("id"),
            "role": machine.get("role"),
            "created_at": machine.get("created_at"),
            "cleanup_observed_at": cleanup.get("observed_at"),
            "lifetime_seconds": lifetime_seconds,
            "rate_milliusd_per_hour": machine.get("rate_milliusd_per_hour"),
            "rate_unit": machine.get("rate_unit"),
            "cost": cost,
        },
        estimate_part,
        actual_part,
    )


def _run_cost(
    machine_reports: list[dict[str, object]], estimates: list[Decimal], actuals: list[Decimal]
) -> dict[str, object]:
    if machine_reports and len(actuals) == len(machine_reports):
        return {
            "status": "actual",
            "source": "provider_usage",
            "unit": "USD*1000",
            "usd": format(sum(actuals, Decimal("0")).normalize(), "f"),
        }
    if machine_reports and len(estimates) == len(machine_reports):
        return {
            "status": "estimated",
            "source": "per_server_rate",
            "unit": "USD*1000 per hour",
            "billing_rounding": "hours rounded up",
            "usd": format(sum(estimates, Decimal("0")).normalize(), "f"),
        }
    return _incomplete_cost()


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
    cleanup_observed_at = _cleanup_proof(cleanup, run_tag, errors)

    raw_machines = manifest.get("machines")
    if not isinstance(raw_machines, list):
        raw_machines = []
        errors.append("manifest_machines_unusable")
    machine_reports: list[dict[str, object]] = []
    estimates: list[Decimal] = []
    actuals: list[Decimal] = []
    cleanup_proven = not {
        "cleanup_observation_timestamp_unusable",
        "cleanup_not_verified",
        "run_owned_machines_not_proven_absent",
        "post_cleanup_servers_used_not_zero",
    }.intersection(errors)
    usage_reliable, usage_by_machine = _usage_by_machine(cleanup, errors)
    for machine in raw_machines:
        if not isinstance(machine, dict):
            errors.append("manifest_machine_unusable")
            continue
        machine_report, estimate, actual = _machine_report(
            machine,
            cleanup,
            cleanup_observed_at,
            cleanup_proven,
            usage_reliable,
            usage_by_machine,
            errors,
        )
        machine_reports.append(machine_report)
        if estimate is not None:
            estimates.append(estimate)
        if actual is not None:
            actuals.append(actual)
    if len(machine_reports) != len(raw_machines) or not machine_reports:
        errors.append("machine_evidence_incomplete")
    if any(machine["cost"]["usd"] is None for machine in machine_reports):
        errors.append("run_cost_incomplete")
    run_cost = _run_cost(machine_reports, estimates, actuals)
    report = {
        "schema_version": "v2",
        "status": "complete" if not errors else "incomplete",
        "run_tag": run_tag,
        "cleanup": cleanup,
        "machines": machine_reports,
        "run_cost": run_cost,
        "incompleteness": sorted(set(errors)),
    }
    (output / "final-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return not errors


def scan_artifact(artifact: Path, *, canaries: tuple[str, ...] = ()) -> list[str]:
    """Return value-free redaction failures for the fixed artifact allow-list."""
    errors: list[str] = []
    if not artifact.is_dir():
        return ["candidate directory is unavailable"]
    allowed = {
        "machines.json",
        "final-report.json",
        *REQUIRED_RUN_FILES,
        REMOTE_INVOCATION_LOG,
        *(f"run/{name}" for name in REQUIRED_RUN_FILES),
        f"run/{REMOTE_INVOCATION_LOG}",
    }
    for path in sorted(artifact.rglob("*")) if artifact.is_dir() else []:
        if path.is_dir():
            if path.relative_to(artifact).as_posix() != "run":
                errors.append("candidate contains an unapproved directory")
            continue
        relative = path.relative_to(artifact).as_posix()
        valid_combination = COMBINATION_LOG.fullmatch(path.name) and (
            path.parent == artifact or path.parent == artifact / "run"
        )
        if relative not in allowed and not valid_combination:
            errors.append("candidate contains an unapproved file")
            continue
        if not path.is_file() or path.is_symlink():
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
    scan.add_argument(
        "--secrets-stdin",
        action="store_true",
        help="read protected NAME=value values from stdin without rendering them",
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
    if args.secrets_stdin:
        canaries += tuple(
            line.partition("=")[2] if "=" in line else line
            for line in __import__("sys").stdin.read().splitlines()
            if (line.partition("=")[2] if "=" in line else line)
        )
    return 0 if not scan_artifact(args.artifact, canaries=canaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
