"""The operation's durable, redacted record: one JSON document and its short report.

The record is written at every phase boundary and before cleanup, so an operation
interrupted anywhere leaves the ids it owns (user, project, story, runs, BotFather
bot) and the facts it had observed. A resumed operation reads it back and goes on
from there instead of ordering or creating anything twice.

Nothing secret is written. Every value an adapter resolves is added to the
operation's one redaction set the moment it is resolved, and every write passes
the whole record through it; token-shaped text (a Telegram bot token, a platform
key, a Fernet envelope) is replaced even when no adapter announced it, so an echo
of a value by a bot or an exception text cannot leave the process either.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import re
from typing import Any

from src.consumers._qa_redaction import QARunRedaction

EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_FILE = "evidence.json"
REPORT_FILE = "report.md"

SECRET_LABEL = "[redacted: synthetic buyer secret]"  # noqa: S105 - a label
PATTERN_LABEL = "[redacted: credential-shaped text]"
#: Credential shapes scrubbed whether or not an adapter announced the value.
_CREDENTIAL_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"cps_[a-z2-7]{12}_[A-Za-z0-9_-]{43}"),  # platform product key
    re.compile(r"gAAAAA[A-Za-z0-9_=-]{20,}"),  # Fernet envelope
)
#: Upper bound of one retained text, after redaction.
MAX_TEXT = 4000


class Phase(StrEnum):
    """The ordered phases of one operation."""

    PREFLIGHT = "preflight"
    REGISTRATION = "registration"
    PRODUCT_TOKEN = "product_token"  # noqa: S105 - a phase name
    ORDER = "order"
    HANDOFF = "handoff"
    BUILD = "build"
    QA_QUIET = "qa_quiet"
    PRODUCT_PROBE = "product_probe"
    PLATFORM = "platform"
    LANGUAGE = "language"
    AUTH_RECHECK = "auth_recheck"
    FREEZE = "freeze"
    TEARDOWN = "teardown"


PHASE_ORDER = tuple(Phase)


class VerdictStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    #: Every observation that was made held, but a required fact stayed unknown.
    INCOMPLETE = "incomplete"


class ObservationStatus(StrEnum):
    OBSERVED = "observed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CleanupStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    #: There was nothing this operation owned to tear down.
    NOTHING_OWNED = "nothing_owned"


#: Every production DoD observation a passed verdict needs, each observed.
REQUIRED_OBSERVATIONS = (
    "brief_frozen",
    "capability_plan_module",
    "preview_after_allowlist",
    "install_after_scaffold",
    "engineering_glue_only",
    "product_ci",
    "deploy_success",
    "qa_passed",
    "product_bot_bound",
    "reply_ru",
    "channels_listed",
    "digest_answered",
    "post_delivered",
    "language_switched",
    "reply_en",
    "auth_key_active",
    "reader_usage",
    "auth_recheck",
)
#: The phase each observation is made in, so a failed one names where it failed.
OBSERVATION_PHASE = {
    **dict.fromkeys(REQUIRED_OBSERVATIONS[:9], Phase.BUILD),
    **dict.fromkeys(REQUIRED_OBSERVATIONS[9:13], Phase.PRODUCT_PROBE),
    **dict.fromkeys(REQUIRED_OBSERVATIONS[13:15], Phase.LANGUAGE),
    **dict.fromkeys(REQUIRED_OBSERVATIONS[15:17], Phase.PLATFORM),
    "auth_recheck": Phase.AUTH_RECHECK,
}


class Redaction:
    """The operation's one redaction set: announced values first, then shapes."""

    def __init__(self) -> None:
        self._values = QARunRedaction(label=SECRET_LABEL)
        #: Texts already scrubbed by the current set; the set only grows, so a new
        #: value empties it.
        self._scrubbed: dict[str, str] = {}

    def add(self, *values: str | None) -> None:
        known = len(self._values.secrets)
        self._values.add(*values, label=SECRET_LABEL)
        if len(self._values.secrets) != known:
            self._scrubbed.clear()

    def text(self, text: str) -> str:
        cached = self._scrubbed.get(text)
        if cached is None:
            cached = self._scrubbed[text] = self._scrub(text)
        return cached

    def _scrub(self, text: str) -> str:
        scrubbed = self._values.text(text)
        for pattern in _CREDENTIAL_PATTERNS:
            scrubbed = pattern.sub(PATTERN_LABEL, scrubbed)
        if len(scrubbed) > MAX_TEXT:
            scrubbed = scrubbed[:MAX_TEXT] + "…"
        return scrubbed

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(key)): self.value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value

    def __repr__(self) -> str:
        return "Redaction()"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def new_record(*, operation_id: str, revision: str, handles: dict[str, str], now: str) -> dict:
    """The record of an operation that has done nothing yet."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "operation_id": operation_id,
        "orchestrator_revision": revision,
        "secret_handles": handles,
        "started_at": now,
        "updated_at": now,
        "phase": Phase.PREFLIGHT.value,
        "completed_phases": [],
        "ids": {},
        "timestamps": {},
        "conversation": {"codegen": [], "botfather": [], "product": [], "decisions": []},
        "watermarks": {},
        "observations": {},
        "verdict": {"status": VerdictStatus.RUNNING.value},
        "cleanup": {"status": CleanupStatus.NOT_STARTED.value},
    }


class EvidenceStore:
    """Atomic, redacted writes of one operation's record into its own directory."""

    def __init__(
        self,
        directory: Path,
        redaction: Redaction,
        *,
        clock: Callable[[], datetime] = utc_now,
        write_text: Callable[[Path, str], None] | None = None,
    ) -> None:
        self.directory = directory
        self.redaction = redaction
        self._clock = clock
        self._write_text = write_text or _atomic_write
        self.record: dict = {}

    @property
    def path(self) -> Path:
        return self.directory / EVIDENCE_FILE

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        record = json.loads(self.path.read_text(encoding="utf-8"))
        if record.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
            raise ValueError("the retained evidence has another schema version")
        self.record = record
        return record

    def now(self) -> str:
        return iso(self._clock())

    def save(self) -> None:
        """Write the redacted record; the live record stays the one the controller edits."""
        self.record["updated_at"] = self.now()
        retained = self.redaction.value(self.record)
        self._write_text(self.path, json.dumps(retained, indent=2, ensure_ascii=False) + "\n")
        self._write_text(self.directory / REPORT_FILE, render_report(retained))

    # --- the record's vocabulary -------------------------------------------

    def enter(self, phase: Phase) -> None:
        self.record["phase"] = phase.value
        self.save()

    def complete(self, phase: Phase) -> None:
        completed = self.record["completed_phases"]
        if phase.value not in completed:
            completed.append(phase.value)
        self.save()

    def is_complete(self, phase: Phase) -> bool:
        return phase.value in self.record["completed_phases"]

    def set_id(self, name: str, value: Any) -> None:
        self.record["ids"][name] = value

    def stamp(self, name: str) -> str:
        moment = self.now()
        self.record["timestamps"][name] = moment
        return moment

    def observe(
        self, name: str, status: ObservationStatus, *, provenance: str, detail: Any = None
    ) -> None:
        self.record["observations"][name] = {
            "status": status.value,
            "provenance": provenance,
            "detail": detail,
            "at": self.now(),
        }

    def message(self, dialog: str, entry: dict) -> None:
        self.record["conversation"][dialog].append(entry)

    def decide(self, entry: dict) -> None:
        self.record["conversation"]["decisions"].append(entry)

    def fail(self, phase: Phase, reason: str, detail: Any = None) -> None:
        """The acceptance verdict is failed at *phase*; a failure is never overwritten."""
        if self.record["verdict"]["status"] == VerdictStatus.FAILED.value:
            return
        self.record["verdict"] = {
            "status": VerdictStatus.FAILED.value,
            "failure_phase": phase.value,
            "reason": reason,
            "detail": detail,
            "at": self.now(),
        }
        self.save()

    def conclude(self) -> str:
        """The verdict from the observations, unless a failure was already recorded."""
        verdict = self.record["verdict"]
        if verdict["status"] == VerdictStatus.FAILED.value:
            return verdict["status"]
        observations = self.record["observations"]
        failed = [
            name
            for name in REQUIRED_OBSERVATIONS
            if observations.get(name, {}).get("status") == ObservationStatus.FAILED.value
        ]
        unknown = [
            name
            for name in REQUIRED_OBSERVATIONS
            if observations.get(name, {}).get("status") != ObservationStatus.OBSERVED.value
            and name not in failed
        ]
        if failed:
            self.record["verdict"] = {
                "status": VerdictStatus.FAILED.value,
                "failure_phase": OBSERVATION_PHASE[failed[0]].value,
                "reason": "observation_failed",
                "detail": {"failed": failed, "unknown": unknown},
                "at": self.now(),
            }
        elif unknown:
            self.record["verdict"] = {
                "status": VerdictStatus.INCOMPLETE.value,
                "reason": "observation_unknown",
                "detail": {"unknown": unknown},
                "at": self.now(),
            }
        else:
            self.record["verdict"] = {"status": VerdictStatus.PASSED.value, "at": self.now()}
        self.save()
        return self.record["verdict"]["status"]

    def cleanup(self, status: CleanupStatus, **facts: Any) -> None:
        self.record["cleanup"] = {"status": status.value, "at": self.now(), **facts}
        self.save()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def render_report(record: dict) -> str:
    """The concise report beside the JSON: verdict, cleanup, ids and observations."""
    verdict = record["verdict"]
    cleanup = record["cleanup"]
    lines = [
        f"# Synthetic buyer operation {record['operation_id']}",
        "",
        f"- Orchestrator revision: `{record['orchestrator_revision']}`",
        f"- Started: {record['started_at']}; updated: {record['updated_at']}",
        f"- Current phase: {record['phase']}; completed: {', '.join(record['completed_phases'])}",
        f"- Acceptance verdict: **{verdict['status']}**"
        + (f" at phase `{verdict['failure_phase']}`" if verdict.get("failure_phase") else "")
        + (f" ({verdict['reason']})" if verdict.get("reason") else ""),
        f"- Cleanup: **{cleanup['status']}**"
        + (f" ({cleanup['reason']})" if cleanup.get("reason") else ""),
        "",
        "## Ids",
        "",
    ]
    lines += [f"- {name}: `{value}`" for name, value in sorted(record["ids"].items())]
    lines += ["", "## Observations", "", "| Observation | Status | Provenance |", "|---|---|---|"]
    observations = record["observations"]
    for name in REQUIRED_OBSERVATIONS:
        entry = observations.get(name)
        status = entry["status"] if entry else ObservationStatus.UNKNOWN.value
        provenance = entry["provenance"] if entry else "not observed"
        lines.append(f"| {name} | {status} | {provenance} |")
    lines.append("")
    return "\n".join(lines)
