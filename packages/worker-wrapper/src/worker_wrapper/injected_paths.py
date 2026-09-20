"""The paths an engineering run adds to a product checkout, in one place.

A worker's `/workspace` is a checkout of a scaffolded product. Everything in it
belongs to that product's Copier kit except the handful of files the
orchestrator puts there: the agent's instructions, the task, the report, the
agent's running notes, the story archive and the venv sentinel. None of them is
the product's, so none of them may reach the product's history.

Three consumers act on exactly this set, and each of them reads it here:

* :func:`write_git_exclude` — the workspace-local `.git/info/exclude` the
  wrapper writes before a turn. It is the only ignore mechanism used, because
  the product's `pre-commit` hook runs `git add -A` and the product's own
  `.gitignore` is a tracked file the orchestrator may not edit.
* :func:`offending_paths` — the publish guard in
  `WorkerWrapper._pushed_completed_result`, which refuses to push a commit that
  carries one of these paths anyway.
* `worker_wrapper.runners.noop` — the scripted no-LLM developer, whose script
  runs in a separate process and therefore receives the same lines as data.

The set holds only paths the orchestrator authors. A product file the
orchestrator no longer touches — the product's `Makefile`, its `AGENTS.md`, its
`.gitignore` — is deliberately absent, so an agent's legitimate edit to one of
them is an ordinary product change that commits and publishes like any other.
"""

from collections.abc import Iterable
import os
from pathlib import Path
import subprocess

import structlog

from shared.constants import WorkerWorkspace
from shared.contracts.vocab import AgentType

logger = structlog.get_logger(__name__)

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

#: The same set as gitignore patterns, anchored to the checkout root so a
#: product file that happens to share a name deeper in the tree stays visible.
EXCLUDE_LINES: tuple[str, ...] = tuple(f"/{path}" for path in INJECTED_PATHS)

EXCLUDE_HEADER = "# orchestrator-injected, never the product's (codegen worker-wrapper)"

_GIT_TIMEOUT_SECONDS = 30


def instruction_filename(agent_type: AgentType) -> str:
    """The workspace-relative file worker-manager injects this agent's instructions into.

    It mirrors `services/worker-manager/src/agents/*.py::get_instruction_path`,
    which reads the same two constants.
    """
    if agent_type == AgentType.CLAUDE:
        return WorkerWorkspace.CLAUDE_INSTRUCTIONS
    return WorkerWorkspace.AGENT_INSTRUCTIONS


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


def git_exclude_path(workspace: str) -> str:
    """Locate the checkout's `.git/info/exclude`, whatever the git layout is."""
    located = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "rev-parse", "--git-path", "info/exclude"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=True,
    )
    relative = located.stdout.strip()
    return relative if os.path.isabs(relative) else os.path.join(workspace, relative)


def write_git_exclude(workspace: str) -> bool:
    """Make the injected paths invisible to `git add -A` in this checkout.

    `.git/info/exclude` is workspace-local, so nothing the product tracks
    changes. Returns whether the rules are in place; a workspace that is not a
    git checkout is reported rather than raised, because the publish guard
    refuses such a commit anyway.
    """
    try:
        path = git_exclude_path(workspace)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        existing = ""
        if os.path.isfile(path):
            existing = Path(path).read_text(encoding="utf-8")
        present = {line.strip() for line in existing.splitlines()}
        missing = [line for line in EXCLUDE_LINES if line not in present]
        if not missing:
            return True
        with open(path, "a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(f"{EXCLUDE_HEADER}\n")
            handle.write("\n".join(missing) + "\n")
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("injected_paths_exclude_failed", workspace=workspace, error=str(error))
        return False
    logger.info("injected_paths_excluded", workspace=workspace, rules=missing)
    return True
