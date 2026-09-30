#!/usr/bin/env python3
"""Retain only the two deployed worker-image generations on a Docker host.

The deployment writes immutable image references to its release record.  This
script trusts those records, not image creation dates: a missing or malformed
record means no worker image is deleted.  Docker still receives ordinary,
non-forced removals, so it independently refuses any image a container starts
using after the inventory was read.  Each image is removed by its ID and on its
own: one that is already gone is logged and skipped, and a failure is reported
per image without stopping the rest.
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
    parent_id: str | None = None


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

    images_by_id = {image.image_id: image for image in images}
    removable_ids = {item.image_id for item in remove}

    def parent_depth(image_id: str, ancestors: frozenset[str] = frozenset()) -> int:
        image = images_by_id[image_id]
        parent_id = image.parent_id
        if parent_id not in removable_ids or parent_id in ancestors:
            return 0
        return 1 + parent_depth(parent_id, ancestors | {image_id})

    # Docker's classic storage driver retains a base while a child exists, so
    # remove stale descendants before their stale ancestors.
    remove.sort(key=lambda item: (-parent_depth(item.image_id), item.image_id))
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
        try:
            raw = json.loads(run_docker(["image", "inspect", image_id]))
        except subprocess.CalledProcessError as error:
            if not _is_gone(error):
                raise
            print(f"GONE {image_id} reason=already_gone")
            continue
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
    while True:
        derived_ids = {
            str(raw["Id"])
            for raw in raw_images
            if raw.get("Parent") in worker_ids and isinstance(raw.get("Id"), str)
        }
        if derived_ids <= worker_ids:
            break
        worker_ids |= derived_ids
    images: list[Image] = []
    for raw in raw_images:
        image_id = raw.get("Id")
        labels = raw.get("Config", {}).get("Labels", {})
        source_hash = labels.get(WORKER_SOURCE_HASH_LABEL) if isinstance(labels, dict) else None
        references = tuple((raw.get("RepoTags") or []) + (raw.get("RepoDigests") or []))
        parent_id = raw.get("Parent")
        if (
            isinstance(image_id, str)
            and isinstance(source_hash, str)
            and source_hash
            and image_id in worker_ids
        ):
            images.append(
                Image(
                    image_id=image_id,
                    source_hash=source_hash,
                    references=references,
                    parent_id=parent_id if isinstance(parent_id, str) and parent_id else None,
                )
            )
    return images


def _container_image_ids(run_docker: Callable[[list[str]], str]) -> set[str]:
    """Return every image ID referenced by running or stopped containers."""
    container_ids = {
        item for item in run_docker(["ps", "-a", "-q", "--no-trunc"]).splitlines() if item
    }
    image_ids: set[str] = set()
    for container_id in container_ids:
        image_id = run_docker(
            ["container", "inspect", "--format", "{{.Image}}", container_id]
        ).strip()
        if not image_id:
            raise RuntimeError(f"Docker returned no image ID for container {container_id}")
        image_ids.add(image_id)
    return image_ids


def _docker_message(error: subprocess.CalledProcessError) -> str:
    return " ".join(" ".join(str(value).split()) for value in (error.stdout, error.stderr) if value)


def _is_gone(error: subprocess.CalledProcessError) -> bool:
    message = _docker_message(error).lower()
    return "no such image" in message or "no such object" in message


def _is_multi_repository(error: subprocess.CalledProcessError) -> bool:
    return "referenced in multiple repositories" in _docker_message(error).lower()


def _is_docker_refusal(error: subprocess.CalledProcessError) -> bool:
    message = _docker_message(error).lower()
    return (
        "conflict:" in message or "being used" in message or "has dependent child images" in message
    )


def _remove_image(image_id: str, run_docker: Callable[[list[str]], str]) -> str:
    """Remove one image by ID, tolerating that it or any of its names is already gone.

    Returns the outcome line. Only a failure docker neither explains as an image that is gone
    nor refuses as one in use starts with ``FAIL``.
    """
    try:
        run_docker(["image", "rm", image_id])
        return f"REMOVED {image_id}"
    except subprocess.CalledProcessError as error:
        if _is_gone(error):
            return f"GONE {image_id} reason=already_gone"
        if not _is_multi_repository(error):
            if _is_docker_refusal(error):
                return f"KEEP {image_id} reason=docker_refused"
            return f"FAIL {image_id} error={_docker_message(error) or error}"
    # Docker refuses an ID several repositories name, so each name it has now is untagged on
    # its own, and the last one removes the image.
    try:
        raw = json.loads(run_docker(["image", "inspect", image_id]))
    except subprocess.CalledProcessError as error:
        if _is_gone(error):
            return f"GONE {image_id} reason=already_gone"
        return f"FAIL {image_id} error={_docker_message(error) or error}"
    names = (raw[0].get("RepoTags") or []) + (raw[0].get("RepoDigests") or [])
    for name in names:
        try:
            run_docker(["image", "rm", name])
        except subprocess.CalledProcessError as error:
            if _is_gone(error):
                print(f"GONE {image_id} reference={name} reason=already_gone")
            elif _is_docker_refusal(error):
                return f"KEEP {image_id} reason=docker_refused"
            else:
                return f"FAIL {image_id} reference={name} error={_docker_message(error) or error}"
    return f"REMOVED {image_id}"


def cleanup_worker_images(
    *,
    release_record: Path,
    previous_release_record: Path,
    dry_run: bool,
    run_docker: Callable[[list[str]], str] = _run_docker,
) -> list[str]:
    """Print the retention decision, then remove only its stale worker images.

    Each image is removed by its ID and on its own, so one that is gone, in use or failing
    does not stop the others. Returns the IDs of the images that failed to be removed.
    """
    images = _worker_images(run_docker)
    running_image_ids = _container_image_ids(run_docker)
    plan = plan_cleanup(
        current_record=_load_record(release_record),
        previous_record=_load_record(previous_release_record),
        images=images,
        running_image_ids=running_image_ids,
    )
    print(render_plan(plan))
    failed: list[str] = []
    if not dry_run:
        for item in plan.remove:
            outcome = _remove_image(item.image_id, run_docker)
            print(f"{outcome} source_hash={item.source_hash}")
            if outcome.startswith("FAIL "):
                failed.append(item.image_id)
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-record", type=Path, required=True)
    parser.add_argument("--previous-release-record", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    failed = cleanup_worker_images(
        release_record=args.release_record,
        previous_release_record=args.previous_release_record,
        dry_run=args.dry_run,
        run_docker=_run_docker,
    )
    if failed:
        print(f"{len(failed)} stale worker images were not removed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
