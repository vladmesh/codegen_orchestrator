#!/usr/bin/env python3
"""Build and inspect the redacted acceptance artifact for one stand run.

The artifact boundary is deliberately narrow.  The runner may collect only its
known report files; this module copies those files, the original public
lifecycle manifest and one derived final report.  It never receives a checkout,
environment file, key, Docker inspection or provider credential.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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
PROVISIONING_DIAGNOSTIC_FILES = (
    "provisioning-state.jsonl",
    "provisioning-services.log",
)
COMBINATION_LOG = re.compile(r"(?:claude|codex)-(?:claude|codex)\.log\Z")
RUN_EVIDENCE = re.compile(r"run-evidence-[a-z0-9-]+-[0-9T+.]+\.json\Z")
# A private-key PEM header is sensitive even when its body was reformatted by a
# traceback or serialized by a logger.  This intentionally does not inspect
# credential names or assignments, so value-free preflight diagnostics remain
# admissible.
_PEM_DASH = r"(?:-|\\x2[dD]|\\u002[dD])"
PRIVATE_KEY_MARKER = re.compile(
    rf"(?:{_PEM_DASH}){{5}}BEGIN [A-Z0-9 ]*PRIVATE KEY(?:{_PEM_DASH}){{5}}",
    re.IGNORECASE,
)
ADMISSION_MARKER = "stand-acceptance-admission-v1"

# These are the only settings whose values are credentials.  The rendered stand
# configuration deliberately also includes public routing and application
# settings, which must never become redaction needles merely because they share
# an environment file with protected values.
PROTECTED_STAND_SECRET_NAMES = frozenset(
    {
        "BITLAUNCH_API_KEY",
        "INTERNAL_API_KEY",
        "POSTGRES_PASSWORD",
        "SECRETS_ENCRYPTION_KEY",
        "LK_JWT_SECRET",
        "REGISTRY_PASSWORD",
        "REGISTRY_PASSWORD_HASH",
        "LOKI_PUSH_PASSWORD",
        "LOKI_PUSH_PASSWORD_HASH",
        "ADMIN_PASSWORD",
        "GRAFANA_DB_PASSWORD",
        "GRAFANA_ADMIN_PASSWORD",
        "WORKER_BROKER_INTERNAL_TOKEN",
        "STAND_CLAUDE_CODE_OAUTH_TOKEN",
        "STAND_CODEX_ACCESS_TOKEN",
        "PO_LLM_API_KEY",
        "ARCHITECT_LLM_API_KEY",
        "GH_APP_PRIVATE_KEY",
        "SSH_PRIVATE_KEY",
    }
)


@dataclass(frozen=True)
class AdmissionIssue:
    """A value-free, operator-actionable reason evidence cannot be uploaded."""

    reason: str
    path: str | None = None
    name: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        issue: dict[str, str | None] = {"path": self.path, "reason": self.reason}
        if self.name is not None:
            issue["name"] = self.name
        return issue


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
            if source.is_file() and RUN_EVIDENCE.fullmatch(source.name):
                shutil.copyfile(source, output / source.name)
        for name in (REMOTE_INVOCATION_LOG, *PROVISIONING_DIAGNOSTIC_FILES):
            source = run_dir / name
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


def _scan_artifact_issues(
    artifact: Path, *, protected_values: tuple[str, ...] = ()
) -> list[AdmissionIssue]:
    """Inspect a fixed candidate without treating diagnostic identifiers as secrets.

    Allowed evidence is free-form diagnostic text.  At this boundary, a secret
    is a supplied protected value or structural private-key PEM material.  The
    narrow file allow-list still keeps configuration and key files out of the
    artifact entirely.
    """
    issues: list[AdmissionIssue] = []
    if not artifact.is_dir():
        return [AdmissionIssue("candidate directory is unavailable")]
    allowed = {
        "machines.json",
        "final-report.json",
        *REQUIRED_RUN_FILES,
        REMOTE_INVOCATION_LOG,
        *PROVISIONING_DIAGNOSTIC_FILES,
        *(f"run/{name}" for name in REQUIRED_RUN_FILES),
        f"run/{REMOTE_INVOCATION_LOG}",
        *(f"run/{name}" for name in PROVISIONING_DIAGNOSTIC_FILES),
    }
    for path in sorted(artifact.rglob("*")) if artifact.is_dir() else []:
        if path.is_dir():
            if path.relative_to(artifact).as_posix() != "run":
                issues.append(
                    AdmissionIssue(
                        "candidate contains an unapproved directory",
                        path.relative_to(artifact).as_posix(),
                    )
                )
            continue
        relative = path.relative_to(artifact).as_posix()
        valid_combination = COMBINATION_LOG.fullmatch(path.name) and (
            path.parent == artifact or path.parent == artifact / "run"
        )
        valid_run_evidence = RUN_EVIDENCE.fullmatch(path.name) and (
            path.parent == artifact or path.parent == artifact / "run"
        )
        if relative not in allowed and not valid_combination and not valid_run_evidence:
            issues.append(AdmissionIssue("candidate contains an unapproved file", relative))
            continue
        if not path.is_file() or path.is_symlink():
            issues.append(AdmissionIssue("candidate contains a non-file entry", relative))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(value and value in text for value in protected_values):
            issues.append(AdmissionIssue("candidate contains a supplied protected value", relative))
        if PRIVATE_KEY_MARKER.search(text):
            issues.append(AdmissionIssue("candidate contains private key material", relative))
    return sorted(set(issues), key=lambda issue: (issue.path or "", issue.reason, issue.name or ""))


def scan_artifact(artifact: Path, *, canaries: tuple[str, ...] = ()) -> list[str]:
    """Return value-free failures for the standalone disposable-canary self-test."""
    issues = _scan_artifact_issues(artifact, protected_values=canaries)
    return sorted(
        {
            "candidate contains a supplied redaction canary"
            if issue.reason == "candidate contains a supplied protected value"
            else issue.reason
            for issue in issues
        }
    )


def protected_values_from_environment(
    environ: dict[str, str] | os._Environ[str],
) -> tuple[str, ...]:
    """Return the complete protected-value set or name its unsafe deficiency."""
    missing = tuple(name for name in sorted(PROTECTED_STAND_SECRET_NAMES) if not environ.get(name))
    if missing:
        raise ProtectedEnvironmentError(missing)
    return tuple(environ[name] for name in sorted(PROTECTED_STAND_SECRET_NAMES))


class ProtectedEnvironmentError(ValueError):
    """The admission job did not receive every protected value it must scan."""

    def __init__(self, missing: tuple[str, ...]):
        self.missing = missing
        super().__init__("protected environment is incomplete")


def _write_admission_status(
    status_path: Path, admitted: bool, issues: tuple[AdmissionIssue, ...] = ()
) -> None:
    """Write a value-free admission state; only ``admitted`` is upload-ready."""
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "marker": ADMISSION_MARKER,
                "status": "admitted" if admitted else "rejected",
                **({"issues": [issue.as_dict() for issue in issues]} if issues else {}),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def admit_artifact(artifact: Path, *, status_path: Path, protected_values: tuple[str, ...]) -> bool:
    """Scan the fixed allow-list and persist the value-free admission status."""
    issues = tuple(_scan_artifact_issues(artifact, protected_values=protected_values))
    admitted = not issues
    _write_admission_status(status_path, admitted, issues)
    return admitted


def admit_artifact_from_environment(
    artifact: Path,
    *,
    status_path: Path,
    environ: dict[str, str] | os._Environ[str],
) -> bool:
    """Admit only when every source-of-truth protected value is available."""
    try:
        protected_values = protected_values_from_environment(environ)
    except ProtectedEnvironmentError as exc:
        issues = tuple(
            AdmissionIssue("protected environment value is missing or empty", name=name)
            for name in exc.missing
        )
        _write_admission_status(status_path, False, issues)
        return False
    return admit_artifact(artifact, status_path=status_path, protected_values=protected_values)


def _emit_admission_diagnostics(status_path: Path, summary_path: Path | None) -> None:
    """Emit only pre-sanitised decision metadata, never a scanned value."""
    payload = _load_json(status_path)
    lines = [
        "stand acceptance admission: " + str(payload.get("status", "unavailable")),
    ]
    for issue in payload.get("issues", []):
        if not isinstance(issue, dict):
            continue
        location = issue.get("path") or issue.get("name") or "candidate"
        reason = issue.get("reason", "admission issue")
        lines.append(f"- {location}: {reason}")
    diagnostic = "\n".join(lines)
    print(diagnostic)
    if summary_path is not None:
        with summary_path.open("a", encoding="utf-8") as summary:
            summary.write(diagnostic + "\n")


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
    admit = sub.add_parser("admit")
    admit.add_argument("--artifact", required=True, type=Path)
    admit.add_argument("--status", required=True, type=Path)
    admit.add_argument(
        "--summary",
        type=Path,
        help="append the value-free admission decision to the job summary",
    )
    admit.add_argument(
        "--protected-env",
        action="store_true",
        help="derive needles only from the fixed protected environment allow-list",
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
    if args.command == "admit":
        if not args.protected_env:
            parser.error("admit requires --protected-env")
        admitted = admit_artifact_from_environment(
            args.artifact,
            status_path=args.status,
            environ=os.environ,
        )
        _emit_admission_diagnostics(args.status, args.summary)
        return 0 if admitted else 2
    canaries = tuple(
        value for name in args.canary_env if (value := os.environ.get(name)) is not None
    )
    return 0 if not scan_artifact(args.artifact, canaries=canaries) else 2


if __name__ == "__main__":
    raise SystemExit(main())
