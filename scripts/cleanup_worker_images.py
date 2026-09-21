#!/usr/bin/env python3
"""Retain only the two deployed worker-image generations on a Docker host.

The deployment writes immutable image references to its release record.  This
script trusts those records, not image creation dates: a missing or malformed
record means no worker image is deleted.  Docker still receives ordinary,
non-forced removals, so it independently refuses any image a container starts
using after the inventory was read.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

WORKER_SOURCE_HASH_LABEL = "org.codegen.worker_source_hash"
WORKER_BASE_IMAGE_NAMES = frozenset(
    {"worker-base-common", "worker-base-claude", "worker-base-factory", "worker-base-codex"}
)


@dataclass(frozen=True)
class Image:
    image_id: str
    source_hash: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    image_id: str
    source_hash: str
    action: str
    reason: str


@dataclass(frozen=True)
class CleanupPlan:
    keep: tuple[Decision, ...]
    remove: tuple[Decision, ...]
    disabled_reason: str | None = None


def _record_details(record: dict[str, Any] | None) -> tuple[str, frozenset[str]] | None:
    if not isinstance(record, dict):
        return None
    source_hash = record.get("source_hash")
    images = record.get("images")
    if not isinstance(source_hash, str) or not source_hash or not isinstance(images, dict):
        return None
    references: set[str] = set()
    if set(images) != WORKER_BASE_IMAGE_NAMES:
        return None
    for name, image in images.items():
        if not isinstance(image, dict):
            return None
        reference = image.get("reference")
        if not isinstance(reference, str):
            return None
        repository, separator, digest = reference.partition("@")
        if (
            not separator
            or repository.rsplit("/", 1)[-1] != name
            or not digest.startswith("sha256:")
        ):
            return None
        references.add(reference)
    if not references:
        return None
    return source_hash, frozenset(references)


def plan_cleanup(
    *,
    current_record: dict[str, Any] | None,
    previous_record: dict[str, Any] | None,
    images: list[Image],
    running_image_ids: set[str],
) -> CleanupPlan:
    """Decide retention before Docker is allowed to remove an image."""
    current = _record_details(current_record)
    if current is None:
        return CleanupPlan((), (), "current_release_record_missing_or_unreadable")
    previous = _record_details(previous_record)
    if previous is None:
        return CleanupPlan((), (), "previous_release_record_missing_or_unreadable")

    current_hash, current_references = current
    previous_hash, previous_references = previous
    deployed_references = current_references | previous_references
    keep: list[Decision] = []
    remove: list[Decision] = []

    for image in images:
        if image.source_hash == current_hash:
            keep.append(Decision(image.image_id, image.source_hash, "KEEP", "current_generation"))
        elif image.source_hash == previous_hash:
            keep.append(Decision(image.image_id, image.source_hash, "KEEP", "previous_generation"))
        elif deployed_references.intersection(image.references):
            keep.append(Decision(image.image_id, image.source_hash, "KEEP", "deployed_record"))
        elif image.image_id in running_image_ids:
            keep.append(Decision(image.image_id, image.source_hash, "KEEP", "running_container"))
        else:
            remove.append(Decision(image.image_id, image.source_hash, "REMOVE", "stale_generation"))

    return CleanupPlan(tuple(keep), tuple(remove))


def render_plan(plan: CleanupPlan) -> str:
    """Render the exact dry-run evidence and the live-run decision log."""
    if plan.disabled_reason:
        return f"KEEP worker-images reason={plan.disabled_reason}"
    return "\n".join(
        f"{item.action} {item.image_id} reason={item.reason} source_hash={item.source_hash}"
        for item in (*plan.keep, *plan.remove)
    )


def _load_record(path: Path) -> dict[str, Any] | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _run_docker(command: list[str]) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker is not installed or is not on PATH")
    return subprocess.run([docker, *command], check=True, capture_output=True, text=True).stdout


def _worker_image_name(reference: str) -> str | None:
    repository = reference.split("@", 1)[0].rsplit(":", 1)[0]
    name = repository.rsplit("/", 1)[-1]
    return name if name in WORKER_BASE_IMAGE_NAMES or name == "worker" else None


def _worker_images(run_docker: Callable[[list[str]], str]) -> list[Image]:
    image_ids = sorted(
        {item for item in run_docker(["image", "ls", "-q", "--no-trunc"]).splitlines() if item}
    )
    raw_images: list[dict[str, Any]] = []
    for image_id in image_ids:
        raw = json.loads(run_docker(["image", "inspect", image_id]))
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise RuntimeError(f"Docker returned an unusable image inspection for {image_id}")
        raw_images.append(raw[0])

    worker_ids = {
        str(raw["Id"])
        for raw in raw_images
        if any(
            _worker_image_name(reference)
            for reference in (raw.get("RepoTags") or []) + (raw.get("RepoDigests") or [])
        )
    }
    images: list[Image] = []
    for raw in raw_images:
        image_id = raw.get("Id")
        labels = raw.get("Config", {}).get("Labels", {})
        source_hash = labels.get(WORKER_SOURCE_HASH_LABEL) if isinstance(labels, dict) else None
        references = tuple((raw.get("RepoTags") or []) + (raw.get("RepoDigests") or []))
        if (
            isinstance(image_id, str)
            and isinstance(source_hash, str)
            and source_hash
            and (image_id in worker_ids or raw.get("Parent") in worker_ids)
        ):
            images.append(Image(image_id=image_id, source_hash=source_hash, references=references))
    return images


def cleanup_worker_images(
    *,
    release_record: Path,
    previous_release_record: Path,
    dry_run: bool,
    run_docker: Callable[[list[str]], str] = _run_docker,
) -> CleanupPlan:
    """Print the retention decision, then remove only its stale worker images."""
    images = _worker_images(run_docker)
    running_image_ids = set(
        run_docker(["ps", "--no-trunc", "--format", "{{.ImageID}}"]).splitlines()
    )
    plan = plan_cleanup(
        current_record=_load_record(release_record),
        previous_record=_load_record(previous_release_record),
        images=images,
        running_image_ids=running_image_ids,
    )
    print(render_plan(plan))
    if not dry_run:
        for item in plan.remove:
            run_docker(["image", "rm", item.image_id])
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-record", type=Path, required=True)
    parser.add_argument("--previous-release-record", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cleanup_worker_images(
        release_record=args.release_record,
        previous_release_record=args.previous_release_record,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
