"""Remove one live run's workspaces, and prove from the filesystem they are gone.

The run's own teardown removes containers, networks, Redis keys, registry
artifacts, the GitHub repository and the database rows. It never removed the
*workspace*: a developer worker's checkout is deliberately preserved across its
own teardown — `worker_removal.delete_worker` logs `workspace_preserved` —
because the next attempt on the same project reuses it. Nothing then takes it
away when the project itself goes, so `/data/workspaces/<repo id>` outlived
every run until the 35-hour workspace garbage collector or the global sweep
happened to pass, and the Definition of Done names a workspace as residue.

So this module is the run-scoped counterpart of that garbage collector: it
removes exactly the entries one run owns, and it reads the filesystem back.

**It runs inside worker-manager**, which is the container the workspace root is
mounted into and the only process that knows where the root is — the live
harness on the control host knows a container path, not a host path, and the
host path is configurable. `SCAFFOLDED_WORKSPACE_PATH` is read with no default,
the same as every other required variable in this repository.

**It refuses anything but a direct child of that root, plus the per-worker plan
directories under `.compose-plans/`.** A workspace identifier arrives from a
run's own manifest, but a remover that walks wherever it is pointed is one typo
away from removing the root, so the containment check is here rather than at the
caller.

**Absence is read, never inferred.** Every removal is followed by a fresh
`exists()` on the same path, and the reported residue is that read — which is
what makes the caller's "asked and found nothing" different from "could not
ask": a failure here exits non-zero and prints no payload at all.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil

#: What the live suite parses this module's answer out of.
WORKSPACE_RESIDUE_MARKER = "WORKSPACE_RESIDUE:"
#: A plan directory is `.compose-plans/<worker id>`: the one nested shape, and
#: therefore the one entry with two path parts.
_PLAN_ENTRY_PARTS = 2
#: The directory worker-manager keeps one compiled compose plan per worker in.
#: It is a child of the workspace root and holds the manager-owned resolved
#: project, so a run's plan directories are as much its residue as its checkout.
PLAN_DIRECTORY = ".compose-plans"


def workspace_root() -> Path:
    """The configured workspace root, or a clear error saying it is not set."""
    base = os.environ.get("SCAFFOLDED_WORKSPACE_PATH")
    if not base:
        raise RuntimeError("SCAFFOLDED_WORKSPACE_PATH is not set")
    return Path(base).resolve()


def resolve_entry(root: Path, entry: str) -> Path:
    """One workspace entry, refused unless it is inside the configured root.

    A direct child is a workspace or a QA scratch directory; `.compose-plans/x`
    is the one nested shape, because that is where the manager writes a worker's
    compiled plan. Everything else — an absolute path, a parent traversal, a
    deeper nesting — is refused rather than interpreted.
    """
    candidate = Path(entry)
    if candidate.is_absolute():
        raise ValueError(f"workspace entry {entry!r} must be relative to {root}")
    parts = candidate.parts
    permitted = len(parts) == 1 or (len(parts) == _PLAN_ENTRY_PARTS and parts[0] == PLAN_DIRECTORY)
    resolved = (root / candidate).resolve()
    if not permitted or not resolved.is_relative_to(root) or resolved == root:
        raise ValueError(
            f"workspace entry {entry!r} must name a direct child of {root} "
            f"or one {PLAN_DIRECTORY}/<worker id> directory"
        )
    return resolved


def present(root: Path, entries: list[str]) -> list[str]:
    """Which of these entries the filesystem still has, as the caller named them."""
    return [entry for entry in entries if resolve_entry(root, entry).exists()]


def remove(root: Path, entries: list[str]) -> list[str]:
    """Remove each entry, then read back which ones are still there.

    `ignore_errors` on the tree walk, because a worker ran as another uid and
    may have left a file this process cannot unlink — and a removal that could
    not finish must be reported as a *present* entry by the read-back, not as a
    raised error that would hide every other entry behind it.
    """
    for entry in entries:
        shutil.rmtree(resolve_entry(root, entry), ignore_errors=True)
    return present(root, entries)


def _emit(payload: dict) -> None:
    print(WORKSPACE_RESIDUE_MARKER + json.dumps(payload, sort_keys=True))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("residue", "cleanup"):
        command = sub.add_parser(name)
        command.add_argument("--entry", action="append", required=True)
    args = parser.parse_args(argv)

    root = workspace_root()
    entries = list(dict.fromkeys(args.entry))
    remaining = remove(root, entries) if args.command == "cleanup" else present(root, entries)
    _emit({"root": str(root), "asked": entries, "findings": remaining})


if __name__ == "__main__":
    main()
