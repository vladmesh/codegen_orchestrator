"""Canonical injected-path publication guard."""

from collections.abc import Iterable

from shared.constants import WorkerWorkspace

#: Workspace-relative paths the orchestrator writes into a product checkout.
#: A trailing slash marks a directory: everything under it is injected too.
INJECTED_PATHS: tuple[str, ...] = (
    WorkerWorkspace.CLAUDE_INSTRUCTIONS,
    WorkerWorkspace.AGENT_INSTRUCTIONS,
    WorkerWorkspace.TASK,
    WorkerWorkspace.REPORT,
    WorkerWorkspace.PROGRESS,
    WorkerWorkspace.VENV_SENTINEL,
    f"{WorkerWorkspace.STORY_DIR}/",
)


def offending_paths(paths: Iterable[str]) -> list[str]:
    """The injected paths among `paths`, sorted, without duplicates."""
    offenders = set()
    for raw in paths:
        candidate = raw.strip()
        if not candidate:
            continue
        for injected in INJECTED_PATHS:
            if injected.endswith("/"):
                if candidate == injected.rstrip("/") or candidate.startswith(injected):
                    offenders.add(candidate)
            elif candidate == injected:
                offenders.add(candidate)
    return sorted(offenders)
