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

logger = structlog.get_logger(__name__)

QA_WORKSPACE_ROOT = "/tmp/qa-runs"  # noqa: S108 — container-local, one dir per run
REPORT_NAME = "QA_REPORT.md"
TRACE_NAME = "tool-trace.jsonl"


@dataclass
class QAWorkspace:
    """An isolated directory for one QA run."""

    path: Path
    destroyed: bool = False
    residual: str | None = None
    _trace: list[dict] = field(default_factory=list)

    @property
    def report_path(self) -> Path:
        return self.path / REPORT_NAME

    @property
    def trace_path(self) -> Path:
        return self.path / TRACE_NAME

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
