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
  and never one a container uses. A missing or unreadable record removes nothing. It
  removes each image by its ID and on its own: an image or name already gone is logged
  and skipped, and a failure is reported per image without stopping the rest.
* ``readback`` is the read-only check after a deploy that production runs the release and
  nothing else: neither the resolved compose configuration nor any container of the compose
  project (running or not, in the configuration or orphaned) bind-mounts checkout source,
  each build service's running container runs the image of the record's digest, and that
  image carries the record's non-empty source hash. It changes nothing.

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
# Stamped on every service image by its build (`--build-arg SOURCE_HASH`), see
# scripts/shared_freshness.py; the release record carries the value its images must have.
SOURCE_HASH_LABEL = "org.codegen.worker_source_hash"
# The deploy path's directories a production container may bind-mount: configuration of
# third-party images and the secrets fallback. Anything else there is checkout source,
# which production runs from the image (docker-compose.prod.yml).
NOT_SOURCE_DIRS = ("infra", "secrets")


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


def _released_images(compose_config: dict[str, Any], release: Release) -> dict[str, str]:
    """Every service compose would build locally, with the release image that replaces it."""
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
    return images


def compose_override(compose_config: dict[str, Any], release: Release) -> str:
    """The compose file that runs every locally built service from the release."""
    images = _released_images(compose_config, release)
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
        try:
            raw = json.loads(run_docker(["image", "inspect", image_id]))
        except subprocess.CalledProcessError as error:
            if not _is_gone(error):
                raise
            print(f"GONE {image_id} reason=already_gone")
            continue
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


def _docker_message(error: subprocess.CalledProcessError) -> str:
    return " ".join(" ".join(str(value).split()) for value in (error.stdout, error.stderr) if value)


def _is_gone(error: subprocess.CalledProcessError) -> bool:
    message = _docker_message(error).lower()
    return "no such image" in message or "no such object" in message


def _is_multi_repository(error: subprocess.CalledProcessError) -> bool:
    return "referenced in multiple repositories" in _docker_message(error).lower()


def _is_docker_refusal(error: subprocess.CalledProcessError) -> bool:
    message = _docker_message(error).lower()
    return "conflict:" in message or "being used" in message


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


def cleanup_service_images(
    *,
    current_record: Path,
    previous_record: Path,
    dry_run: bool,
    run_docker: Callable[[list[str]], str] = _run_docker,
) -> list[str]:
    """Print the retention decision, then remove only the stale service images.

    Each image is removed on its own, so one that is gone, in use or failing does not stop
    the others. Returns the IDs of the images that failed to be removed.
    """
    plan = plan_cleanup(
        current=load_release(current_record),
        previous=load_release(previous_record),
        images=_images(run_docker),
        container_image_ids=_container_image_ids(run_docker),
    )
    print(render_plan(plan))
    failed: list[str] = []
    if not dry_run:
        for item in plan.remove:
            outcome = _remove_image(item.image_id, run_docker)
            print(outcome)
            if outcome.startswith("FAIL "):
                failed.append(item.image_id)
    return failed


# --- readback -----------------------------------------------------------------------------


def _checkout_source(source: str, deploy_path: Path) -> str | None:
    """The deploy-path-relative path a bind source names, when it is checkout source."""
    try:
        relative = Path(os.path.normpath(source)).relative_to(deploy_path)
    except ValueError:
        return None
    if relative.parts and relative.parts[0] in NOT_SOURCE_DIRS:
        return None
    return str(relative)


def _inspect_container(container_id: str, run_docker: Callable[[list[str]], str]) -> dict:
    raw = json.loads(run_docker(["container", "inspect", container_id]))
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise RuntimeError(f"Docker returned an unusable inspection for {container_id}")
    return raw[0]


def _project_source_mounts(
    project: str,
    services: dict[str, Any],
    deploy_path: Path,
    run_docker: Callable[[list[str]], str],
) -> tuple[list[str], set[str]]:
    """The checkout-source bind mounts of every container of the compose project.

    Every container docker keeps under the project's label counts, running or only created,
    and whether or not the current configuration still names its service: a container left
    from an older configuration keeps the mounts it was created with. Returns the problems
    and the IDs of the containers that have them.
    """
    container_ids = sorted(
        {
            item
            for item in run_docker(
                [
                    "ps",
                    "-a",
                    "-q",
                    "--no-trunc",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                ]
            ).split()
            if item
        }
    )
    problems: list[str] = []
    source_bound: set[str] = set()
    for container_id in container_ids:
        container = _inspect_container(container_id, run_docker)
        labels = (container.get("Config") or {}).get("Labels") or {}
        service_name = labels.get("com.docker.compose.service") or "(no service label)"
        where = f"{service_name} {container_id[:12]}"
        if service_name not in services:
            where += " (orphan)"
        for mount in container.get("Mounts") or []:
            source = mount.get("Source", "") if mount.get("Type") == "bind" else ""
            path = _checkout_source(source, deploy_path) if source else None
            if path is not None:
                problems.append(
                    f"{where}: bind-mounts checkout source {path} at {mount.get('Destination')}"
                )
                source_bound.add(container_id)
    if not source_bound:
        print(f"OK {len(container_ids)} containers of project {project} mount no checkout source")
    return problems, source_bound


def _without_override(compose_config: dict[str, Any], release: Release) -> dict[str, Any]:
    """The configuration as the base files state it, whether or not the override was applied.

    The readback is run on the live stack, which carries the override, so a build service's
    image is already a release reference; it maps back to the local name the override
    replaced, and every other image stays what it is.
    """
    by_reference = {reference: name for name, reference in release.references.items()}
    services = {}
    for service_name, service in (compose_config.get("services") or {}).items():
        image = service.get("image") if isinstance(service, dict) else None
        if isinstance(image, str) and image in by_reference:
            local = f"{LOCAL_IMAGE_PREFIX}{by_reference[image]}:{LOCAL_BUILD_TAG}"
            service = {**service, "image": local}
        services[service_name] = service
    return {**compose_config, "services": services}


def readback(
    *,
    compose_config: dict[str, Any],
    record: Path,
    deploy_path: Path,
    run_docker: Callable[[list[str]], str],
) -> list[str]:
    """Every reason production does not run exactly the recorded release, empty when it does.

    Prints one line per container it confirmed. Asks docker only to list and inspect.
    """
    release = load_release(record)
    if release is None:
        raise ServiceReleaseError(f"{record} is not a usable service release record")
    source_hash = json.loads(record.read_text(encoding="utf-8"))["source_hash"]
    project = compose_config.get("name")
    if not isinstance(project, str) or not project:
        raise ServiceReleaseError("the compose configuration names no project")
    services = compose_config.get("services") or {}
    problems: list[str] = []

    for service_name, service in sorted(services.items()):
        for volume in service.get("volumes") or []:
            source = volume.get("source", "") if volume.get("type") == "bind" else ""
            path = _checkout_source(source, deploy_path) if source else None
            if path is not None:
                problems.append(
                    f"config: service {service_name} bind-mounts checkout source {path} "
                    f"at {volume.get('target')}"
                )

    mount_problems, source_bound = _project_source_mounts(
        project, services, deploy_path, run_docker
    )
    problems.extend(mount_problems)

    for service_name, reference in _released_images(
        _without_override(compose_config, release), release
    ).items():
        expected_id = run_docker(["image", "inspect", "--format", "{{.Id}}", reference]).strip()
        containers = run_docker(
            [
                "ps",
                "-q",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service_name}",
            ]
        ).split()
        if not containers:
            if services[service_name].get("scale") == 0:
                print(f"SKIP {service_name} scale=0")
            else:
                problems.append(f"{service_name}: no running container")
            continue
        for container_id in containers:
            container = _inspect_container(container_id, run_docker)
            where = f"{service_name} {container_id[:12]}"
            failed = False
            if container.get("Image") != expected_id:
                problems.append(
                    f"{where}: runs image {container.get('Image')}, the record's {reference} "
                    f"is {expected_id}"
                )
                failed = True
            stamped = ((container.get("Config") or {}).get("Labels") or {}).get(SOURCE_HASH_LABEL)
            if not stamped or stamped != source_hash:
                problems.append(
                    f"{where}: carries {SOURCE_HASH_LABEL}={stamped!r}, the record's is "
                    f"{source_hash!r}"
                )
                failed = True
            if container_id in source_bound:
                failed = True
            if not failed:
                print(f"OK {where} image={reference} {SOURCE_HASH_LABEL}={stamped}")
    return problems


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

    check = commands.add_parser(
        "readback", help="read-only: production runs the recorded release and no source"
    )
    check.add_argument("--compose-config", type=Path, required=True)
    check.add_argument("--record", type=Path, required=True)
    check.add_argument("--deploy-path", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "rotate":
            rotate_previous_record(args.current_record, args.previous_record, args.next_revision)
        elif args.command == "compose-override":
            write_compose_override(args.compose_config, args.record, args.output)
        elif args.command == "readback":
            problems = readback(
                compose_config=json.loads(args.compose_config.read_text(encoding="utf-8")),
                record=args.record,
                deploy_path=Path(os.path.abspath(args.deploy_path)),
                run_docker=_run_docker,
            )
            for problem in problems:
                print(f"FAIL {problem}")
            if problems:
                return 1
            print("production runs the recorded release and mounts no checkout source")
        else:
            failed = cleanup_service_images(
                current_record=args.current_record,
                previous_record=args.previous_record,
                dry_run=args.dry_run,
                run_docker=_run_docker,
            )
            if failed:
                print(f"{len(failed)} stale service images were not removed", file=sys.stderr)
                return 1
    except ServiceReleaseError as error:
        print(f"FATAL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
