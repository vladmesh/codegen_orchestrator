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
# The redacted service log tails the runner collects when the suite itself
# failed, the counterpart of the provisioning-failure diagnostics above.
SUITE_DIAGNOSTIC_FILES = ("suite-services.log",)
# The bounded, redacted snapshot taken from the *target* host — the machine the
# application itself runs on — when a run's evidence says a successful deploy
# was followed by a deployed URL that answered nobody.  The orchestrator's own
# services cannot say whether the application container was down, up but
# unreachable, or answering something QA rejected; this file is where that half
# of the answer lives, and the run evidence is what asks for it.
TARGET_DIAGNOSTIC_FILES = ("target-app.log",)
# The harness's own post-mortem dump, written beside the run evidence so it
# reaches the handoff instead of dying with the ephemeral host.
# The dump name is `debug-<test name>-<date>-<time>.md`, and a test name is a
# Python identifier or a prefix built from one, so underscores and capitals are
# names the codebase can actually produce.  Anything else still shaped like a
# dump is not quietly left behind: `DEBUG_DUMP_CANDIDATE` catches it so the
# drop is named in the final report rather than being silently absent.
DEBUG_DUMP = re.compile(r"debug-[A-Za-z0-9_-]+-\d{8}-\d{6}\.md\Z")
DEBUG_DUMP_CANDIDATE = re.compile(r"debug-.*\.md\Z")
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
            if source.is_file() and DEBUG_DUMP.fullmatch(source.name):
                shutil.copyfile(source, output / source.name)
            elif source.is_file() and DEBUG_DUMP_CANDIDATE.fullmatch(source.name):
                errors.append(f"debug_dump_name_unadmissible:{source.name}")
        for name in (
            REMOTE_INVOCATION_LOG,
            *PROVISIONING_DIAGNOSTIC_FILES,
            *SUITE_DIAGNOSTIC_FILES,
            *TARGET_DIAGNOSTIC_FILES,
        ):
            source = run_dir / name
            if source.is_file():
                shutil.copyfile(source, output / source.name)


def _capture_is_stated(capture: object) -> bool:
    """Whether one evidence field is a collected value or a stated absence.

    The run-evidence artifact never carries a bare empty field: every fact is
    either ``captured`` with a value or ``missed`` with the reason it could not
    be read.  Both are admissible — a piece that genuinely could not be
    collected is named with why.  Anything else is silence, and silence about a
    paid failure is what this admission refuses.
    """
    if not isinstance(capture, dict):
        return False
    if capture.get("status") == "captured":
        return capture.get("value") is not None
    return capture.get("status") == "missed" and bool(capture.get("reason"))


def _paid_failure_errors(name: str, artifact: dict[str, Any]) -> list[str]:
    """Refuse a paid failure whose artifact cannot say where it stopped or why."""
    failure = artifact.get("failure")
    verdict = artifact.get("verdict")
    if not isinstance(failure, dict) or not isinstance(verdict, dict):
        return [f"run_evidence_failure_section_missing:{name}"]
    if not verdict.get("paid") or not failure.get("failed"):
        return []
    errors: list[str] = []
    if not failure.get("stage") or not failure.get("failure_kind"):
        errors.append(f"paid_failure_stage_missing:{name}")
    if not _capture_is_stated(failure.get("control_plane_reason")):
        errors.append(f"paid_failure_reason_missing:{name}")
    engineering = artifact.get("engineering")
    if not isinstance(engineering, dict) or not _capture_is_stated(engineering.get("collection")):
        errors.append(f"paid_failure_engineering_evidence_missing:{name}")
    qa = artifact.get("qa")
    if not isinstance(qa, dict) or not _capture_is_stated(qa.get("run_record")):
        errors.append(f"paid_failure_qa_run_record_missing:{name}")
    deployment = artifact.get("deployment")
    if not isinstance(deployment, dict) or not _capture_is_stated(deployment.get("run_record")):
        errors.append(f"paid_failure_deploy_run_record_missing:{name}")
    errors += _reachability_errors(name, deployment)
    if not verdict.get("reasons"):
        errors.append(f"paid_failure_verdict_silent:{name}")
    return errors


def _reachability_errors(name: str, deployment: object) -> list[str]:
    """Refuse a paid failure that cannot say what the deployed URL answered.

    The three reads that separate "the app was down", "the app was up but
    unreachable from the orchestrator" and "the app answered something QA
    rejected" are the deploy's own smoke, the harness's probe and QA's probe.
    Each is admissible as a stated missed capture — an unread probe is a
    finding — but none of them may simply be absent.
    """
    if not isinstance(deployment, dict):
        return [f"paid_failure_reachability_missing:{name}"]
    reachability = deployment.get("reachability")
    if not isinstance(reachability, dict):
        return [f"paid_failure_reachability_missing:{name}"]
    errors = [
        f"paid_failure_reachability_read_missing:{name}:{read}"
        for read in ("deploy_smoke", "harness_probe", "qa_probe")
        if not _capture_is_stated(reachability.get(read))
    ]
    snapshot = reachability.get("target_host_snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("reason"):
        errors.append(f"paid_failure_target_snapshot_requirement_missing:{name}")
    return errors


def target_snapshot_required(artifact: dict[str, Any]) -> bool:
    """Whether one run-evidence artifact asks for a snapshot of the target host.

    The artifact decides — this only reads its answer — so the step that
    collects and the boundary that refuses cannot disagree about which runs owe
    a target-host snapshot.
    """
    deployment = artifact.get("deployment")
    reachability = deployment.get("reachability") if isinstance(deployment, dict) else None
    snapshot = reachability.get("target_host_snapshot") if isinstance(reachability, dict) else None
    return bool(isinstance(snapshot, dict) and snapshot.get("required"))


def run_dir_needs_target_snapshot(run_dir: Path) -> bool:
    """Whether any collected run evidence in one runner directory asks for one.

    Unreadable or absent evidence answers "no": the artifact is what asks, so a
    directory that holds no answerable artifact asks for nothing here.  It is
    refused elsewhere — a paid failure with no readable evidence is already an
    admission error of its own.
    """
    if not run_dir.is_dir():
        return False
    for path in sorted(run_dir.iterdir()):
        if not path.is_file() or not RUN_EVIDENCE.fullmatch(path.name):
            continue
        try:
            artifact = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if target_snapshot_required(artifact):
            return True
    return False


def _uncollected_debug_dumps(output: Path, artifact: dict[str, Any]) -> list[str]:
    """Name every dump the run wrote that did not reach the artifact.

    The run-evidence artifact records the dumps the harness wrote, so the two
    halves can be compared here: a name the run declared and the handoff does
    not carry is a piece that could not be collected, and it is named with why
    instead of disappearing between the stand and the reader.
    """
    declared = artifact.get("debug_dumps")
    if not isinstance(declared, list):
        return []
    return [
        f"debug_dump_not_collected:{name}"
        for name in declared
        if isinstance(name, str) and not (output / name).is_file()
    ]


def _run_evidence_errors(output: Path) -> list[str]:
    """Read back every collected run-evidence artifact and fail closed on a gap.

    This is the same admission the workflow already fails closed on, extended to
    the one thing it could not see before: a paid run that failed after
    provisioning succeeded and whose artifact names neither the failing stage
    nor the control plane's reason for it.  Such an artifact is refused here
    rather than uploaded as if it explained anything.
    """
    errors: list[str] = []
    evidence = [path for path in sorted(output.iterdir()) if RUN_EVIDENCE.fullmatch(path.name)]
    paid_failure = False
    wants_target_snapshot = False
    for path in evidence:
        try:
            artifact = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            errors.append(f"run_evidence_unreadable:{path.name}")
            continue
        failure = artifact.get("failure")
        verdict = artifact.get("verdict")
        if isinstance(failure, dict) and isinstance(verdict, dict):
            paid_failure = paid_failure or bool(verdict.get("paid") and failure.get("failed"))
        wants_target_snapshot = wants_target_snapshot or target_snapshot_required(artifact)
        errors += _paid_failure_errors(path.name, artifact)
        errors += _uncollected_debug_dumps(output, artifact)
    if paid_failure and not any((output / name).is_file() for name in SUITE_DIAGNOSTIC_FILES):
        errors.append("paid_failure_service_diagnostics_missing")
    if wants_target_snapshot and not any(
        (output / name).is_file() for name in TARGET_DIAGNOSTIC_FILES
    ):
        errors.append("paid_failure_target_snapshot_missing")
    return errors


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
    errors += _run_evidence_errors(output)

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
        *SUITE_DIAGNOSTIC_FILES,
        *(f"run/{name}" for name in SUITE_DIAGNOSTIC_FILES),
        *TARGET_DIAGNOSTIC_FILES,
        *(f"run/{name}" for name in TARGET_DIAGNOSTIC_FILES),
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
        valid_debug_dump = DEBUG_DUMP.fullmatch(path.name) and (
            path.parent == artifact or path.parent == artifact / "run"
        )
        if (
            relative not in allowed
            and not valid_combination
            and not valid_run_evidence
            and not valid_debug_dump
        ):
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
    needs = sub.add_parser("needs-target-snapshot")
    needs.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "needs-target-snapshot":
        return 0 if run_dir_needs_target_snapshot(args.run_dir) else 1
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
