#!/usr/bin/env python3
"""The deployed control-plane service release on a deploy host: its records, the compose
override that runs it, and the cleanup that keeps it and its predecessor.

`infra/scripts/pull-service-images.sh` pulls and verifies the release of one revision and
leaves its record, `deployed-service-images.json`; the record it replaced is kept as
`previous-deployed-service-images.json`, the rollback target. Everything here trusts only
those records, and only once they parse as a release record:

* ``rotate`` keeps the record being replaced as the previous one, when it is a valid
  record of another revision (the same rule `rotate_worker_image_records.py` applies to
  the worker chain, keyed by revision instead of source hash, because every revision has
  its own service release).
* ``compose-override`` turns the record into the compose file that makes every service
  compose would build locally run its released `<repository>@sha256:...` instead. It
  reads the contour's resolved compose configuration (`docker compose ... config --format
  json`), so a build service the release does not cover fails the deploy before anything
  running is touched, rather than being built on the host.
* ``cleanup`` removes service images of neither the current nor the previous release,
  and never one a container uses. A missing or unreadable record removes nothing.

It runs on the deploy host with the system interpreter, so it is stdlib-only.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

SCHEMA_VERSION = 1
# The name compose gives a service's local build (docker-compose.yml), and the local name
# pull-service-images.sh gives a verified release image: codegen-orchestrator/<image>:<sha>.
LOCAL_IMAGE_PREFIX = "codegen-orchestrator/"
LOCAL_BUILD_TAG = "local"
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class ServiceReleaseError(RuntimeError):
    """The record or the compose configuration cannot be turned into a deployment."""


@dataclass(frozen=True)
class Release:
    git_sha: str
    references: dict[str, str]  # image name -> <repository>@sha256:...

    def local_names(self) -> frozenset[str]:
        return frozenset(f"{LOCAL_IMAGE_PREFIX}{name}:{self.git_sha}" for name in self.references)


def parse_release(record: object) -> Release | None:
    """The release a record describes, or None when it is not a usable release record."""
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        return None
    git_sha = record.get("git_sha")
    source_hash = record.get("source_hash")
    images = record.get("images")
    if not isinstance(git_sha, str) or not GIT_SHA.match(git_sha):
        return None
    if not isinstance(source_hash, str) or not source_hash:
        return None
    if not isinstance(images, dict) or not images:
        return None
    references: dict[str, str] = {}
    for name, entry in images.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("reference"), str):
            return None
        reference = entry["reference"]
        repository, _, digest = reference.partition("@")
        if not repository.endswith(f"/{name}") or not digest.startswith("sha256:"):
            return None
        references[name] = reference
    return Release(git_sha=git_sha, references=references)


def load_release(path: Path) -> Release | None:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parse_release(parsed)


# --- rotate -------------------------------------------------------------------------------


def rotate_previous_record(current: Path, previous: Path, next_revision: str) -> None:
    """Keep the current record as the previous one when it is a valid, other release."""
    if not GIT_SHA.match(next_revision):
        raise ServiceReleaseError(f"next revision {next_revision!r} is not a full git SHA")
    release = load_release(current)
    if release is None:
        # An unreadable current record must not leave a stale previous record trusted.
        previous.write_text("", encoding="utf-8")
        return
    if release.git_sha != next_revision:
        shutil.copyfile(current, previous)


# --- compose-override ---------------------------------------------------------------------


def compose_override(compose_config: dict[str, Any], release: Release) -> str:
    """The compose file that runs every locally built service from the release."""
    services = compose_config.get("services")
    if not isinstance(services, dict) or not services:
        raise ServiceReleaseError("the compose configuration names no services")
    images: dict[str, str] = {}
    for service_name, service in sorted(services.items()):
        if not isinstance(service, dict) or "build" not in service:
            continue
        image = service.get("image")
        name, _, tag = (
            image.removeprefix(LOCAL_IMAGE_PREFIX).rpartition(":")
            if isinstance(image, str) and image.startswith(LOCAL_IMAGE_PREFIX)
            else ("", "", "")
        )
        if not name or tag != LOCAL_BUILD_TAG:
            raise ServiceReleaseError(
                f"service {service_name} builds locally as {image!r}, not as "
                f"{LOCAL_IMAGE_PREFIX}<image>:{LOCAL_BUILD_TAG}, so no release image names it"
            )
        if name not in release.references:
            raise ServiceReleaseError(
                f"service {service_name} builds {name}, which the release of "
                f"{release.git_sha} does not contain; it would have to be built on the host"
            )
        images[service_name] = release.references[name]
    if not images:
        raise ServiceReleaseError("the compose configuration builds no service locally")
    lines = [
        f"# Generated by scripts/service_release.py from the release of {release.git_sha}.",
        "# Every service compose would build locally runs its released image instead.",
        "services:",
    ]
    for service_name, reference in images.items():
        lines += [f"  {json.dumps(service_name)}:", f"    image: {json.dumps(reference)}"]
    return "\n".join(lines) + "\n"


def write_compose_override(compose_config: Path, record: Path, output: Path) -> None:
    release = load_release(record)
    if release is None:
        raise ServiceReleaseError(f"{record} is not a usable service release record")
    try:
        config = json.loads(compose_config.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ServiceReleaseError(
            f"{compose_config} is not a compose configuration: {error}"
        ) from error
    if not isinstance(config, dict):
        raise ServiceReleaseError(f"{compose_config} is not a compose configuration")
    staged = output.with_name(output.name + ".next")
    staged.write_text(compose_override(config, release), encoding="utf-8")
    os.replace(staged, output)


# --- cleanup ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Image:
    image_id: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    image_id: str
    action: str
    reason: str
    references: tuple[str, ...]


@dataclass(frozen=True)
class CleanupPlan:
    keep: tuple[Decision, ...]
    remove: tuple[Decision, ...]
    disabled_reason: str | None = None


def _service_image_name(reference: str, repositories: dict[str, str]) -> str | None:
    """The chain image a local reference names, or None when it is not a service image."""
    repository = reference.partition("@")[0]
    if "@" in reference:
        return repositories.get(repository)
    name, _, _tag = reference.removeprefix(LOCAL_IMAGE_PREFIX).rpartition(":")
    if reference.startswith(LOCAL_IMAGE_PREFIX) and name in repositories.values():
        return name
    return None


def plan_cleanup(
    *,
    current: Release | None,
    previous: Release | None,
    images: list[Image],
    container_image_ids: set[str],
) -> CleanupPlan:
    """Decide what to remove before Docker is asked to remove anything."""
    if current is None:
        return CleanupPlan((), (), "current_release_record_missing_or_unreadable")
    if previous is None:
        return CleanupPlan((), (), "previous_release_record_missing_or_unreadable")
    repositories = {
        reference.partition("@")[0]: name
        for release in (current, previous)
        for name, reference in release.references.items()
    }
    kept_references = (
        set(current.references.values())
        | set(previous.references.values())
        | current.local_names()
        | previous.local_names()
    )
    keep: list[Decision] = []
    remove: list[Decision] = []
    for image in images:
        if not any(_service_image_name(ref, repositories) for ref in image.references):
            continue
        if kept_references.intersection(image.references):
            keep.append(Decision(image.image_id, "KEEP", "deployed_release", image.references))
        elif image.image_id in container_image_ids:
            keep.append(Decision(image.image_id, "KEEP", "container", image.references))
        else:
            remove.append(Decision(image.image_id, "REMOVE", "stale_release", image.references))
    return CleanupPlan(tuple(keep), tuple(remove))


def render_plan(plan: CleanupPlan) -> str:
    if plan.disabled_reason:
        return f"KEEP service-images reason={plan.disabled_reason}"
    return "\n".join(
        f"{item.action} {item.image_id} reason={item.reason} references={','.join(item.references)}"
        for item in (*plan.keep, *plan.remove)
    )


def _run_docker(command: list[str]) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker is not installed or is not on PATH")
    return subprocess.run([docker, *command], check=True, capture_output=True, text=True).stdout


def _images(run_docker: Callable[[list[str]], str]) -> list[Image]:
    image_ids = sorted(
        {item for item in run_docker(["image", "ls", "-q", "--no-trunc"]).splitlines() if item}
    )
    images = []
    for image_id in image_ids:
        raw = json.loads(run_docker(["image", "inspect", image_id]))
        if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
            raise RuntimeError(f"Docker returned an unusable image inspection for {image_id}")
        references = tuple((raw[0].get("RepoTags") or []) + (raw[0].get("RepoDigests") or []))
        images.append(Image(image_id=str(raw[0]["Id"]), references=references))
    return images


def _container_image_ids(run_docker: Callable[[list[str]], str]) -> set[str]:
    """Every image ID a running or stopped container was created from."""
    container_ids = {
        item for item in run_docker(["ps", "-a", "-q", "--no-trunc"]).splitlines() if item
    }
    image_ids = set()
    for container_id in container_ids:
        image_id = run_docker(
            ["container", "inspect", "--format", "{{.Image}}", container_id]
        ).strip()
        if not image_id:
            raise RuntimeError(f"Docker returned no image ID for container {container_id}")
        image_ids.add(image_id)
    return image_ids


def _is_docker_refusal(error: subprocess.CalledProcessError) -> bool:
    message = "\n".join(str(value) for value in (error.stdout, error.stderr) if value).lower()
    return "conflict:" in message or "being used" in message


def cleanup_service_images(
    *,
    current_record: Path,
    previous_record: Path,
    dry_run: bool,
    run_docker: Callable[[list[str]], str] = _run_docker,
) -> CleanupPlan:
    """Print the retention decision, then remove only the stale service images."""
    plan = plan_cleanup(
        current=load_release(current_record),
        previous=load_release(previous_record),
        images=_images(run_docker),
        container_image_ids=_container_image_ids(run_docker),
    )
    print(render_plan(plan))
    if not dry_run:
        for item in plan.remove:
            # By every name it has: removal by ID refuses an image several repositories name.
            try:
                run_docker(["image", "rm", *item.references])
            except subprocess.CalledProcessError as error:
                if not _is_docker_refusal(error):
                    raise
                print(f"KEEP {item.image_id} reason=docker_refused")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    rotate = commands.add_parser("rotate", help="keep the replaced record as the previous one")
    rotate.add_argument("--current-record", type=Path, required=True)
    rotate.add_argument("--previous-record", type=Path, required=True)
    rotate.add_argument("--next-revision", required=True)

    override = commands.add_parser("compose-override", help="write the compose image override")
    override.add_argument("--compose-config", type=Path, required=True)
    override.add_argument("--record", type=Path, required=True)
    override.add_argument("--output", type=Path, required=True)

    cleanup = commands.add_parser("cleanup", help="remove service images of no kept release")
    cleanup.add_argument("--current-record", type=Path, required=True)
    cleanup.add_argument("--previous-record", type=Path, required=True)
    cleanup.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "rotate":
            rotate_previous_record(args.current_record, args.previous_record, args.next_revision)
        elif args.command == "compose-override":
            write_compose_override(args.compose_config, args.record, args.output)
        else:
            cleanup_service_images(
                current_record=args.current_record,
                previous_record=args.previous_record,
                dry_run=args.dry_run,
            )
    except ServiceReleaseError as error:
        print(f"FATAL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
