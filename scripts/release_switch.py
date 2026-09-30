#!/usr/bin/env python3
"""The pending release set of a deploy, checked before the host switch and promoted after it.

The deploy's verify step pulls and verifies both image releases of a revision from a
staged worktree and leaves what it verified in a pending directory that no container
mounts (`<deploy path>/.release-pending/`, PENDING_FILES). Nothing it writes is live.
The `Switch` step of .github/workflows/deploy.yml is the one place live host state
changes, and this is its helper at both ends:

* ``check`` runs first, before any live write: the pending set is complete and is the
  release of the revision being deployed. It is the only check inside the switch.
  The switch runs it from the pending directory's own copy of this file, because the
  deploy path still holds the previous revision's scripts at that point, so ``check``
  imports nothing beyond the standard library.
* ``promote`` runs last, only after `up` succeeded. It rotates the LIVE current records
  into previous — the pending set is never rotated anywhere — and then moves the pending
  records into place. The live records are the only truth of what was last brought up
  successfully; a failed attempt before this point leaves them exactly as they were.

It runs on the deploy host with the system interpreter, so it is stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any

WORKER_RECORD = "deployed-worker-images.json"
SERVICE_RECORD = "deployed-service-images.json"
PREVIOUS_WORKER_RECORD = "previous-deployed-worker-images.json"
PREVIOUS_SERVICE_RECORD = "previous-deployed-service-images.json"
OVERRIDE = "deployed-service-images.compose.yml"
PENDING_FILES = (WORKER_RECORD, SERVICE_RECORD, OVERRIDE)
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST_REFERENCE = re.compile(r"[\"']?([^\s\"']+@sha256:[0-9a-f]+)[\"']?")


class SwitchError(RuntimeError):
    """The pending set cannot be switched to."""


def _record(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SwitchError(f"{path} is not a readable release record: {error}") from error
    if not isinstance(record, dict) or not isinstance(record.get("images"), dict):
        raise SwitchError(f"{path} is not a release record")
    return record


def _references(record: dict[str, Any]) -> set[str]:
    return {
        entry["reference"]
        for entry in record["images"].values()
        if isinstance(entry, dict) and isinstance(entry.get("reference"), str)
    }


def check_pending(pending: Path, revision: str) -> None:
    """Refuse a pending set that is incomplete or is not the release of `revision`."""
    if not GIT_SHA.match(revision):
        raise SwitchError(f"revision {revision!r} is not a full git SHA")
    missing = [name for name in PENDING_FILES if not (pending / name).is_file()]
    if missing:
        raise SwitchError(f"the pending set in {pending} is incomplete: no {', '.join(missing)}")
    worker = _record(pending / WORKER_RECORD)
    service = _record(pending / SERVICE_RECORD)
    for name, record in (("worker", worker), ("service", service)):
        if record.get("git_sha") != revision:
            raise SwitchError(
                f"the pending {name} record is the release of {record.get('git_sha')!r}, "
                f"not of {revision}"
            )
    source_hash = worker.get("source_hash")
    if not isinstance(source_hash, str) or not source_hash:
        raise SwitchError("the pending worker record carries no source hash")
    if service.get("source_hash") != source_hash:
        raise SwitchError(
            f"the pending worker record has source hash {source_hash!r} and the service "
            f"record {service.get('source_hash')!r}: they are not one tree's releases"
        )
    override = set(DIGEST_REFERENCE.findall((pending / OVERRIDE).read_text(encoding="utf-8")))
    if not override or not override <= _references(service):
        raise SwitchError(
            f"the pending compose override names {sorted(override - _references(service))} "
            "beyond the pending service release"
        )


def promote(pending: Path, live: Path, revision: str) -> None:
    """After `up`: live current becomes previous, the pending records become current."""
    # Imported here: `check` runs from a lone copy of this file with no siblings.
    try:
        from scripts.rotate_worker_image_records import (
            rotate_previous_record as rotate_worker,
        )
        from scripts.service_release import rotate_previous_record as rotate_service
    except ModuleNotFoundError:  # Script execution puts scripts/, not the root, on sys.path.
        from rotate_worker_image_records import rotate_previous_record as rotate_worker
        from service_release import rotate_previous_record as rotate_service

    source_hash = _record(pending / WORKER_RECORD).get("source_hash")
    if not isinstance(source_hash, str) or not source_hash:
        raise SwitchError("the pending worker record carries no source hash")
    # Rotation reads only the live current records. Each is a no-op for the same
    # generation (worker: source hash, service: revision), so a redeploy keeps previous.
    rotate_worker(live / WORKER_RECORD, live / PREVIOUS_WORKER_RECORD, source_hash)
    rotate_service(live / SERVICE_RECORD, live / PREVIOUS_SERVICE_RECORD, revision)
    os.replace(pending / WORKER_RECORD, live / WORKER_RECORD)
    os.replace(pending / SERVICE_RECORD, live / SERVICE_RECORD)
    shutil.rmtree(pending)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="refuse an incomplete or foreign pending set")
    check.add_argument("--pending", type=Path, required=True)
    check.add_argument("--revision", required=True)
    promoted = commands.add_parser("promote", help="rotate the live records, then promote")
    promoted.add_argument("--pending", type=Path, required=True)
    promoted.add_argument("--live", type=Path, required=True)
    promoted.add_argument("--revision", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            check_pending(args.pending, args.revision)
            print(f"The pending release set of {args.revision} is complete.")
        else:
            promote(args.pending, args.live, args.revision)
            print(f"{args.revision} is the deployed release; the one it replaced is previous.")
    except SwitchError as error:
        print(f"FATAL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
