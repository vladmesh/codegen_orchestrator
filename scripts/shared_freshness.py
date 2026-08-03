#!/usr/bin/env python3
"""Is what is built behind the tree on `shared`?

`shared` reaches its consumers through three channels (docs/REBUILD.md). Two of them
cannot go stale: a bind-mounted container picks up an edit on restart, and a test run
imports `shared` from the tree over PYTHONPATH. Only an image that bakes `shared` into
itself with `COPY shared` can hold an old copy, and until now nothing compared such an
image with the tree it was built from.

The comparison reuses the mechanism the worker images already have: a hash of the baked
sources, passed into the build as `--build-arg SOURCE_HASH` and kept on the image as the
`org.codegen.worker_source_hash` label. This module is the single place that hash is
computed; the Makefile reads it from here (`WORKER_SOURCE_HASH`), so there is one counter
and not two that can drift apart.

Two rules, and both fail closed:

* Coverage is static. Every Dockerfile in the tree with a `COPY shared` has to declare
  `ARG SOURCE_HASH` and the label. A new one that does not is uncovered, and the check
  fails naming it — on a clean machine too, because reading Dockerfiles needs no docker.
* Freshness is per image. For every tracked image the label is compared with the hash of
  the tree. A missing, empty or unparsable label fails the check naming the image and the
  reason: an image that bakes `shared` and cannot say which `shared` it baked is exactly
  the silent pass this check exists to remove.

An image that is not built is not behind anything — there is no copy of `shared` to be
old — so it is reported as NOT_BUILT and does not fail the check. That is what keeps the
check green on a clean machine and in CI, where nothing is built.

The tracked set is derived, never listed by hand: the worker base images come from the
`rebuild-worker-images` recipe in the Makefile, and the compose services come from
docker-compose.yml. A compose service that bakes `shared` without mounting `./shared`
over it has to name its image explicitly, otherwise its image name depends on the compose
project name and the check cannot find it — a service like that with no `image:` fails
the check.

Images built by the compose files under docker/test/ are out of the tracked set: every
make target behind them builds with `--build`, so a test run cannot pick up a stale copy.
They still have to carry the label, by the coverage rule above.

Usage:
    python3 scripts/shared_freshness.py hash    # the tree hash, for the Makefile
    uv run python scripts/shared_freshness.py check
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]

# Set on an image by --build-arg SOURCE_HASH; already read back at runtime by
# worker-manager (services/worker-manager/src/image_builder.py).
SOURCE_HASH_LABEL = "org.codegen.worker_source_hash"

# What the hash covers. Wider than the `shared` tree on purpose: the worker images bake
# worker-wrapper and their own definitions alongside it, and one hash over the union is
# cheaper than one hash per image. An edit to worker-wrapper therefore also marks the
# worker-manager image stale, which costs a rebuild and never hides one.
HASHED_TREES = ("shared", "packages/worker-wrapper", "services/worker-manager/images")
HASH_LENGTH = 16
HASH_RE = re.compile(rf"^[0-9a-f]{{{HASH_LENGTH}}}$")

COPY_SHARED = re.compile(r"^\s*COPY\s+(?:--\S+\s+)*shared/?\s", re.MULTILINE)
DECLARES_ARG = re.compile(r"^\s*ARG\s+SOURCE_HASH\b", re.MULTILINE)
DECLARES_LABEL = re.compile(
    rf"^\s*LABEL\s+{re.escape(SOURCE_HASH_LABEL)}=\$\{{?SOURCE_HASH\}}?\s*$",
    re.MULTILINE,
)

WALK_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
SHARED_MOUNT = "./shared:/app/shared"
WORKER_IMAGES_RECIPE = "rebuild-worker-images"


# --- the tree hash ----------------------------------------------------------


def _hashed_files(root: Path) -> list[str]:
    files = []
    for tree in HASHED_TREES:
        for path in (root / tree).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            files.append(str(path.relative_to(root)))
    return sorted(files)


def source_hash(root: Path = REPO_ROOT) -> str:
    """sha256 over the sources baked into images, truncated the way the Makefile did.

    The digest is taken over `sha256sum` lines so that a rename changes it as much as an
    edit does.
    """
    digest = hashlib.sha256()
    for name in _hashed_files(root):
        file_digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        digest.update(f"{file_digest}  {name}\n".encode())
    return digest.hexdigest()[:HASH_LENGTH]


# --- coverage: which Dockerfiles bake shared, and do they say so ------------


def dockerfiles_baking_shared(root: Path = REPO_ROOT) -> list[str]:
    found = []
    for path in root.glob("**/Dockerfile*"):
        if WALK_SKIP_DIRS & set(path.parts) or not path.is_file():
            continue
        if COPY_SHARED.search(path.read_text()):
            found.append(str(path.relative_to(root)))
    return sorted(found)


def uncovered_dockerfiles(root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """Dockerfiles that bake `shared` without declaring what they baked."""
    uncovered = []
    for name in dockerfiles_baking_shared(root):
        text = (root / name).read_text()
        if not DECLARES_ARG.search(text):
            uncovered.append((name, "bakes shared but declares no ARG SOURCE_HASH"))
        elif not DECLARES_LABEL.search(text):
            uncovered.append((name, f"bakes shared but sets no LABEL {SOURCE_HASH_LABEL}"))
    return uncovered


# --- the tracked images -----------------------------------------------------


@dataclass(frozen=True)
class TrackedImage:
    reference: str
    dockerfile: str
    origin: str


def _recipe_commands(makefile: str, target: str) -> list[str]:
    """The shell commands of one Makefile recipe, with continuations joined."""
    body = re.search(rf"^{re.escape(target)}:.*?\n((?:\t.*\n|\n)*)", makefile, re.MULTILINE)
    if body is None:
        raise RuntimeError(f"Makefile has no {target} recipe to read image names from")
    joined = body.group(1).replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip()]


def worker_base_images(root: Path = REPO_ROOT) -> list[TrackedImage]:
    """The images `make rebuild-worker-images` produces, read off the recipe itself."""
    images = []
    for command in _recipe_commands((root / "Makefile").read_text(), WORKER_IMAGES_RECIPE):
        if "docker build" not in command:
            continue
        dockerfile = re.search(r"-f\s+(\S+)", command)
        tags = [tag for tag in re.findall(r"-t\s+(\S+)", command) if tag.endswith(":latest")]
        if dockerfile is None or not tags:
            raise RuntimeError(f"cannot read image name out of: {command}")
        images.append(TrackedImage(tags[0], dockerfile.group(1), f"make {WORKER_IMAGES_RECIPE}"))
    return images


def _compose_services_baking_shared(root: Path, bakers: set[str]) -> list[tuple[str, dict]]:
    import yaml  # only the check needs it; `hash` runs on a bare interpreter

    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    services = []
    for name, service in compose["services"].items():
        dockerfile = service.get("build", {}).get("dockerfile")
        if dockerfile not in bakers:
            continue
        if any(SHARED_MOUNT in str(volume) for volume in service.get("volumes", [])):
            # The mount covers the baked copy: what runs is the tree, always.
            continue
        services.append((name, service))
    return services


def compose_images(root: Path = REPO_ROOT) -> list[TrackedImage]:
    """Compose services whose baked `shared` is what actually runs."""
    images = []
    bakers = set(dockerfiles_baking_shared(root))
    for name, service in _compose_services_baking_shared(root, bakers):
        reference = service.get("image")
        if not reference:
            raise RuntimeError(
                f"compose service {name} bakes shared without mounting ./shared over it, "
                "so its image has to be checked, but it declares no image: name — without "
                "one the image name depends on the compose project name"
            )
        if ":" not in reference.rsplit("/", 1)[-1]:
            reference = f"{reference}:latest"
        images.append(TrackedImage(reference, service["build"]["dockerfile"], f"compose {name}"))
    return images


def tracked_images(root: Path = REPO_ROOT) -> list[TrackedImage]:
    return worker_base_images(root) + compose_images(root)


# --- the check --------------------------------------------------------------

NOT_BUILT = None  # nothing built holds no copy of `shared`, so nothing to be behind


def docker_image_labels(reference: str) -> dict[str, str] | None:
    """Labels of a local image, or NOT_BUILT. Reads the local daemon, builds nothing."""
    result = subprocess.run(
        [  # noqa: S607
            "docker",
            "image",
            "inspect",
            reference,
            "--format",
            "{{json .Config.Labels}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        if "No such image" in result.stderr:
            return NOT_BUILT
        raise RuntimeError(f"docker image inspect {reference} failed: {result.stderr.strip()}")
    return json.loads(result.stdout) or {}


def _image_problem(image: TrackedImage, labels: dict[str, str], expected: str) -> str | None:
    stored = labels.get(SOURCE_HASH_LABEL)
    if stored is None:
        return f"{image.reference} ({image.origin}) is built without a {SOURCE_HASH_LABEL} label"
    if not stored.strip():
        return f"{image.reference} ({image.origin}) carries an empty {SOURCE_HASH_LABEL}"
    if not HASH_RE.match(stored):
        return (
            f"{image.reference} ({image.origin}) carries {SOURCE_HASH_LABEL}={stored!r}, "
            "which is not a source hash"
        )
    if stored != expected:
        return f"{image.reference} ({image.origin}) was built from {stored}, the tree is {expected}"
    return None


def check(
    root: Path = REPO_ROOT,
    inspect=docker_image_labels,
    report=print,
) -> list[str]:
    """Every reason the built stand is behind the tree, empty when it is not."""
    problems = [f"{name}: {reason}" for name, reason in uncovered_dockerfiles(root)]
    if problems:
        return problems

    expected = source_hash(root)
    for image in tracked_images(root):
        labels = inspect(image.reference)
        if labels is NOT_BUILT:
            report(f"   {image.reference}: not built, nothing to be behind")
            continue
        problem = _image_problem(image, labels, expected)
        if problem is None:
            report(f"   {image.reference}: matches the tree")
        else:
            problems.append(problem)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["hash", "check"])
    command = parser.parse_args(argv).command

    if command == "hash":
        print(source_hash())
        return 0

    print(f"🔍 shared source hash: {source_hash()}")
    problems = check()
    if problems:
        print("❌ what is built is behind the tree on shared:")
        for problem in problems:
            print(f"   - {problem}")
        print("   Fix with: make build (compose images) / make rebuild-worker-images")
        return 1
    print("✅ nothing built is behind the tree on shared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
