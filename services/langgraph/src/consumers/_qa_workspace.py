"""The per-run scratch space a central QA run owns, and its destruction.

Nothing a QA run produces belongs to the QA service: the report the agent
writes, the trace of what it did, and any temporary material live in a
directory created for this run and removed when the run ends — including when
it ends by raising or by being cancelled.

Removal is read back rather than assumed. `QAWorkspace.destroyed` and
`residual` are what the runner reports; a directory that survived is residue
with a name, not a silent leak.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import tempfile

import structlog

from shared.contracts.dto.run_result import (
    QABlocker,
    QAProbeFileKind,
    QAProbePlatform,
    QAProbeRun,
    QATelegramProbeEvidence,
)

logger = structlog.get_logger(__name__)

QA_WORKSPACE_ROOT = "/tmp/qa-runs"  # noqa: S108 — container-local, one dir per run
REPORT_NAME = "QA_REPORT.md"
TRACE_NAME = "tool-trace.jsonl"
VERDICT_NAME = "verdict.json"
MAX_PROBES = 50
MAX_PROBE_TEXT = 20_000
MAX_PROBE_DURATION_MS = 65_000
MAX_PROBE_EXIT_STATUS = 255
MAX_PROBE_NAME = 256
MAX_PROBE_ARGUMENTS = 64
MAX_PROBE_ARGUMENT_BYTES = 8192
PROBE_TRUNCATION_MARKER = "\n...[truncated]"


@dataclass(frozen=True)
class ProductObservation:
    """One successful read of the deployed product's own output.

    Recorded by the runtime when a call came back with something the product
    answered — a route that responded, a file that was read, a bot that
    replied. A fire and its evidence read are not observations: both answer
    with the core's record of the dispatch, which says nothing about what any
    provider did with the event.

    `subject` is what was read, in the words the request used, so a check that
    claims to rest on this read can be matched against it.

    Not every observation recorded here can be bound to a criterion. A package
    behaviour's row is bound only by an HTTP read of a route the criterion's
    observable names (`observation_answers` in `agents.qa.packages`), so a bot
    reply recorded here is product output for every other purpose in this run
    and still does not answer a package behaviour's observable. That is a known
    accepted limitation of the package path, not an oversight: see
    `docs/CONTRACTS.md`.
    """

    position: int
    tool: str
    subject: str


@dataclass(frozen=True)
class BehaviourEvidence:
    """What the product's own record of one fired behaviour said.

    `position` is where in this run the read happened, because evidence read
    before its fire is evidence of something else. `dispatch_status` is the
    product's own account of the event: `dispatched` once it was emitted,
    `undelivered` when it never was — and an event that was never emitted
    cannot have been consumed by anything.
    """

    position: int
    dispatch_status: str


@dataclass
class QAWorkspace:
    """An isolated directory for one QA run."""

    path: Path
    destroyed: bool = False
    residual: str | None = None
    verdict: str | None = None
    telegram_probe_evidence: list[QATelegramProbeEvidence] = field(default_factory=list)
    probe_runs: list[QAProbeRun] = field(default_factory=list)
    telegram_probe_blocker: QABlocker | None = None
    #: Behaviours the product accepted a fire for during this run, mapped to
    #: how many calls this run had made when it accepted them. Written by the
    #: runtime, so "the behaviour was never fired" and "nothing was read after
    #: the fire" are both decided here rather than from anything an executor
    #: reports about itself.
    fired_behaviours: dict[str, int] = field(default_factory=dict)
    #: Behaviours whose recorded command this run read back. Written by the
    #: runtime when the product answered `job_evidence` with a command, so "the
    #: run never read the evidence" is the runner's own fact and not an
    #: executor's account of itself.
    behaviour_evidence: dict[str, BehaviourEvidence] = field(default_factory=dict)
    #: Every successful read of the product's own output this run made, in
    #: order. Written by the runtime, so what a run looked at is the runner's
    #: fact and not an executor's account of itself.
    observations: list[ProductObservation] = field(default_factory=list)
    _trace: list[dict] = field(default_factory=list)

    @property
    def report_path(self) -> Path:
        return self.path / REPORT_NAME

    @property
    def trace_path(self) -> Path:
        return self.path / TRACE_NAME

    @property
    def verdict_path(self) -> Path:
        return self.path / VERDICT_NAME

    @property
    def transport_refusals(self) -> list[QATelegramProbeEvidence]:
        """Inputs the runtime refused because the transport cannot carry them.

        Today that is `telegram_probe` refusing an empty or whitespace-only
        message: a message operation with nothing to send, never delivered and
        with no error, which no real probe records. These are the only grounds
        on which an executor's not-applicable check is accepted.
        """
        return [
            evidence
            for evidence in self.telegram_probe_evidence
            if evidence.action == "message"
            and evidence.delivered is False
            and evidence.error is None
            and not evidence.sent.strip()
        ]

    def submit_verdict(self, raw: str) -> None:
        """Keep the executor's final result JSON, whoever the executor was.

        A verdict submitted through the capability endpoint lands here rather
        than travelling on the worker output stream: the developer-worker result
        contract describes a commit, and a QA run has none. The runtime holding
        the verdict in its own workspace also means the run is judged from what
        the runner received, not from a container's exit status.
        """
        self.verdict = raw
        self.verdict_path.write_text(raw, encoding="utf-8")

    def record(self, tool: str, request: str, response: str) -> None:
        """Append one runner-owned line of evidence about what the agent did.

        The trace is written by the runtime, not by the agent: it is the record
        the write guard is decided from, so the thing being watched must not be
        able to author it.
        """
        entry = {"tool": tool, "request": request[:4000], "response": response[:4000]}
        self._trace.append(entry)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def record_fired_behaviour(self, name: str) -> None:
        """Note that the product accepted a fire of `name`, and where in the run."""
        self.fired_behaviours.setdefault(name, len(self._trace))

    def record_behaviour_evidence(self, name: str, dispatch_status: str) -> None:
        """Note what the product's own record of `name` said, and when it was read.

        The product answers an evidence read with a command only within the
        product that fired it, so this is the one read bound to this run, this
        deployment and this behaviour.
        """
        self.behaviour_evidence.setdefault(
            name, BehaviourEvidence(position=len(self._trace), dispatch_status=dispatch_status)
        )

    def record_observation(self, tool: str, subject: str) -> None:
        """Note that this run read the product's own output, and what it read."""
        self.observations.append(
            ProductObservation(position=len(self._trace), tool=tool, subject=subject)
        )

    def record_telegram_probe(
        self, evidence: QATelegramProbeEvidence, blocker: QABlocker | None = None
    ) -> None:
        """Retain runner-owned Telegram evidence until it is persisted on the run."""
        self.telegram_probe_evidence.append(evidence)
        if blocker is not None and self.telegram_probe_blocker is None:
            self.telegram_probe_blocker = blocker

    def record_probe(  # noqa: PLR0911, PLR0913 - the retained record is the typed endpoint contract
        self,
        *,
        platform: str,
        name: str,
        source: str,
        arguments: list[str],
        stdout: str,
        stderr: str,
        exit_status: int,
        duration_ms: int,
        source_truncated: bool = False,
        stdout_truncated: bool = False,
        stderr_truncated: bool = False,
        file_kind: str | None = None,
    ) -> dict:
        """Validate and retain a sandbox probe without trusting its account."""
        if len(self.probe_runs) >= MAX_PROBES:
            return {"error": f"at most {MAX_PROBES} probes may be retained per run"}
        if not isinstance(platform, str) or platform not in {
            item.value for item in QAProbePlatform
        }:
            return {"error": "platform must be one of telegram, http, web"}
        if file_kind is not None and (
            not isinstance(file_kind, str)
            or file_kind not in {item.value for item in QAProbeFileKind}
        ):
            return {"error": "file_kind must be py or sh"}
        if not isinstance(name, str) or not name.strip():
            return {"error": "name must be a non-empty string"}
        if len(name) > MAX_PROBE_NAME:
            return {"error": f"name must be at most {MAX_PROBE_NAME} characters"}
        if not all(isinstance(value, str) for value in (source, stdout, stderr)):
            return {"error": "source, stdout and stderr must be strings"}
        if not isinstance(arguments, list) or not all(
            isinstance(value, str) for value in arguments
        ):
            return {"error": "arguments must be a list of strings"}
        if len(arguments) > MAX_PROBE_ARGUMENTS:
            return {"error": f"at most {MAX_PROBE_ARGUMENTS} arguments may be retained"}
        if sum(len(value) for value in arguments) > MAX_PROBE_ARGUMENT_BYTES:
            return {"error": "probe arguments exceed the total size limit"}
        if not all(
            isinstance(value, bool)
            for value in (source_truncated, stdout_truncated, stderr_truncated)
        ):
            return {"error": "truncation flags must be booleans"}
        if (
            isinstance(exit_status, bool)
            or not isinstance(exit_status, int)
            or not -MAX_PROBE_EXIT_STATUS <= exit_status <= MAX_PROBE_EXIT_STATUS
        ):
            return {
                "error": (
                    f"exit_status must be an integer between -{MAX_PROBE_EXIT_STATUS} "
                    f"and {MAX_PROBE_EXIT_STATUS}"
                )
            }
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not 0 <= duration_ms <= MAX_PROBE_DURATION_MS
        ):
            return {"error": f"duration_ms must be between 0 and {MAX_PROBE_DURATION_MS}"}

        def bounded(value: str, already_truncated: bool) -> tuple[str, bool]:
            if len(value) <= MAX_PROBE_TEXT:
                return value, already_truncated
            return (
                value[: MAX_PROBE_TEXT - len(PROBE_TRUNCATION_MARKER)] + PROBE_TRUNCATION_MARKER,
                True,
            )

        bounded_source, source_truncated = bounded(source, source_truncated)
        bounded_stdout, stdout_truncated = bounded(stdout, stdout_truncated)
        bounded_stderr, stderr_truncated = bounded(stderr, stderr_truncated)
        probe = QAProbeRun(
            id=f"probe-{len(self.probe_runs) + 1}",
            platform=platform,
            name=name.strip(),
            source=bounded_source,
            arguments=arguments,
            stdout=bounded_stdout,
            stderr=bounded_stderr,
            exit_status=exit_status,
            duration_ms=duration_ms,
            source_truncated=source_truncated,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            file_kind=file_kind,
        )
        self.probe_runs.append(probe)
        return {"id": probe.id}

    def trace_text(self) -> str:
        """The whole trace as one blob, for scanning."""
        return "\n".join(
            f"{entry['tool']} {entry['request']} {entry['response']}" for entry in self._trace
        )

    def write_report(self, markdown: str) -> None:
        self.report_path.write_text(markdown, encoding="utf-8")

    def read_report(self) -> str:
        if not self.report_path.exists():
            return ""
        return self.report_path.read_text(encoding="utf-8")

    def destroy(self) -> None:
        """Remove the directory and read back whether it is gone."""
        shutil.rmtree(self.path, ignore_errors=True)
        if self.path.exists():
            self.residual = f"QA workspace {self.path} survived removal"
            self.destroyed = False
        else:
            self.residual = None
            self.destroyed = True
        logger.info(
            "qa_workspace_destroyed",
            path=str(self.path),
            destroyed=self.destroyed,
            residual=self.residual,
        )


@contextmanager
def qa_workspace(root: str | None = None) -> Iterator[QAWorkspace]:
    """Create a workspace for one run and destroy it however the run ends."""
    root = root or QA_WORKSPACE_ROOT
    Path(root).mkdir(parents=True, exist_ok=True)
    workspace = QAWorkspace(path=Path(tempfile.mkdtemp(prefix="qa-run-", dir=root)))
    workspace.trace_path.touch()
    logger.info("qa_workspace_created", path=str(workspace.path))
    try:
        yield workspace
    finally:
        workspace.destroy()
