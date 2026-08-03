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
computed; the Makefile reads it from here (`WORKER_SOURCE_HASH`) and so do the fixtures
that build worker base images (`tests/integration/backend/conftest.py`,
`tests/e2e/conftest.py`), so there is one counter and not several that can drift apart.

Coverage is derived from the tree and never listed by hand, and everything this module
cannot read reliably fails the check instead of passing quietly:

* Every Dockerfile in the repository is parsed. One that copies `shared` has to declare
  `ARG SOURCE_HASH` and the label. A `COPY` whose sources cannot be read — JSON form that
  does not parse, a source built out of a variable, a glob in place of the top directory —
  fails the check naming the file, because "we could not find `shared` in it" is not the
  same statement as "it does not bake `shared`".
* Every compose file in the repository is parsed, `docker/test/**` included. A service
  built from a Dockerfile that bakes `shared` has to pass `SOURCE_HASH` in `build.args`
  and to declare an explicit `image:` — without a name of its own the image is called
  after the compose project and nothing can find it again. Neither rule needs docker.
* For every tracked image the label is compared with the hash of the tree. A missing,
  empty or unparsable label fails the check naming the image and the reason: an image that
  bakes `shared` and cannot say which `shared` it baked is exactly the silent pass this
  check exists to remove.

Two things are deliberately not stale, and both say so where the code decides it:

* An image that is not built is not behind anything — there is no copy of `shared` to be
  old — so it is reported as NOT_BUILT and does not fail the check. That is what keeps the
  check green on a clean machine and in CI, where nothing is built.
* A compose service that mounts `./shared` over `/app/shared` runs the tree, not the copy
  in its image, so its image is not compared. It still has to be nameable and to stamp its
  hash, so the day the mount goes away the check works without being taught anything.

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
import shlex
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]

# Set on an image by --build-arg SOURCE_HASH; already read back at runtime by
# worker-manager (services/worker-manager/src/image_builder.py).
SOURCE_HASH_LABEL = "org.codegen.worker_source_hash"
BUILD_ARG = "SOURCE_HASH"

# What the hash covers. Wider than the `shared` tree on purpose: the worker images bake
# worker-wrapper and their own definitions alongside it, and one hash over the union is
# cheaper than one hash per image. An edit to worker-wrapper therefore also marks the
# worker-manager image stale, which costs a rebuild and never hides one.
HASHED_TREES = ("shared", "packages/worker-wrapper", "services/worker-manager/images")
HASH_LENGTH = 16
HASH_RE = re.compile(rf"^[0-9a-f]{{{HASH_LENGTH}}}$")

DECLARES_ARG = re.compile(rf"^\s*ARG\s+{BUILD_ARG}\b", re.MULTILINE)
DECLARES_LABEL = re.compile(
    rf"^\s*LABEL\s+{re.escape(SOURCE_HASH_LABEL)}=\$\{{?{BUILD_ARG}\}}?\s*$",
    re.MULTILINE,
)

WALK_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__"}
SHARED_TREE = "shared"
SHARED_MOUNT_TARGET = "/app/shared"
WORKER_IMAGES_RECIPE = "rebuild-worker-images"
GLOB_CHARS = set("*?[")
SOURCE_AND_DESTINATION = 2  # the shortest COPY and the shortest volume mapping


class Unreadable(RuntimeError):
    """Something in the tree cannot be read reliably.

    Raised instead of guessing: a Dockerfile or a compose file this module cannot parse
    is a hole in the coverage, and a hole fails the check by name.
    """


# --- the tree hash ----------------------------------------------------------


def _hashed_files(root: Path) -> list[str]:
    files = []
    for tree in HASHED_TREES:
        for path in (root / tree).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            files.append(str(path.relative_to(root)))
    return sorted(files)


def source_hash(root: Path | str = REPO_ROOT) -> str:
    """sha256 over the sources baked into images, truncated the way the Makefile did.

    The digest is taken over `sha256sum` lines so that a rename changes it as much as an
    edit does. This is the only producer of the value written as `SOURCE_HASH`.
    """
    root = Path(root)
    digest = hashlib.sha256()
    for name in _hashed_files(root):
        file_digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        digest.update(f"{file_digest}  {name}\n".encode())
    return digest.hexdigest()[:HASH_LENGTH]


# --- reading Dockerfiles ----------------------------------------------------


def _instructions(text: str) -> list[str]:
    """Dockerfile instructions with line continuations joined and comments dropped."""
    instructions: list[str] = []
    carried = ""
    for raw in text.splitlines():
        line = raw.strip()
        if carried:
            if line.startswith("#"):  # docker drops a comment line inside a continuation
                continue
        elif not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            carried += line[:-1].strip() + " "
            continue
        instructions.append((carried + line).strip())
        carried = ""
    if carried.strip():
        instructions.append(carried.strip())
    return instructions


def _first_segment(source: str) -> str:
    parts = [part for part in source.replace("\\", "/").split("/") if part not in ("", ".")]
    return parts[0] if parts else "."


def _copy_sources(instruction: str, where: str) -> list[str]:
    """The sources of one COPY, in whichever form docker accepts it.

    Shell form, JSON-array form and (already joined by _instructions) line continuations
    all end up here. A source this cannot resolve raises rather than being skipped.
    """
    rest = instruction[len("COPY") :].strip()
    while rest.startswith("--"):
        _flag, _, rest = rest.partition(" ")
        rest = rest.strip()

    if rest.startswith("["):
        try:
            tokens = json.loads(rest)
        except json.JSONDecodeError as error:
            raise Unreadable(
                f"{where}: COPY in JSON form that does not parse ({error.msg}): {instruction!r}"
            ) from error
        if not isinstance(tokens, list) or not all(isinstance(token, str) for token in tokens):
            raise Unreadable(f"{where}: COPY in JSON form that is not a list of paths: {rest!r}")
    else:
        try:
            tokens = shlex.split(rest)
        except ValueError as error:
            raise Unreadable(f"{where}: COPY that does not tokenize: {instruction!r}") from error

    if len(tokens) < SOURCE_AND_DESTINATION:
        raise Unreadable(f"{where}: COPY without a source and a destination: {instruction!r}")

    sources = tokens[:-1]
    for source in sources:
        if "$" in source:
            raise Unreadable(
                f"{where}: COPY source {source!r} is built out of a variable, so what it "
                "copies cannot be read from the tree"
            )
        if GLOB_CHARS & set(_first_segment(source)):
            raise Unreadable(
                f"{where}: COPY source {source!r} starts with a glob, so what it copies "
                "cannot be read from the tree"
            )
    return sources


def dockerfile_bakes_shared(text: str, where: str) -> bool:
    bakes = False
    for instruction in _instructions(text):
        if not re.match(r"COPY\b", instruction, re.IGNORECASE):
            continue
        for source in _copy_sources(instruction, where):
            if _first_segment(source) == SHARED_TREE:
                bakes = True
    return bakes


def dockerfiles_baking_shared(root: Path = REPO_ROOT) -> list[str]:
    """Every Dockerfile in the tree that copies `shared` into the image."""
    found = []
    for path in root.glob("**/Dockerfile*"):
        if WALK_SKIP_DIRS & set(path.parts) or not path.is_file():
            continue
        name = str(path.relative_to(root))
        if dockerfile_bakes_shared(path.read_text(), name):
            found.append(name)
    return sorted(found)


def uncovered_dockerfiles(root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """Dockerfiles that bake `shared` without declaring what they baked."""
    uncovered = []
    for name in dockerfiles_baking_shared(root):
        text = (root / name).read_text()
        if not DECLARES_ARG.search(text):
            uncovered.append((name, f"bakes shared but declares no ARG {BUILD_ARG}"))
        elif not DECLARES_LABEL.search(text):
            uncovered.append((name, f"bakes shared but sets no LABEL {SOURCE_HASH_LABEL}"))
    return uncovered


# --- reading compose files --------------------------------------------------


def _compose_documents(root: Path) -> list[tuple[Path, dict]]:
    """Every compose file in the tree, docker/test/** included, with its services.

    A compose file is recognised by having a `services:` mapping, not by its name or its
    directory, so a new one is covered the day it is added. One that has `services:` and
    still cannot be parsed raises: unreadable is not the same as having nothing to check.
    """
    import yaml  # only the check needs it; `hash` runs on a bare interpreter

    class _Loader(yaml.SafeLoader):
        """SafeLoader that tolerates compose's own tags, `!reset` in the prod override."""

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)

    documents = []
    for path in sorted(root.rglob("*.y*ml")):
        if WALK_SKIP_DIRS & set(path.parts) or not path.is_file():
            continue
        if path.suffix not in (".yml", ".yaml"):
            continue
        text = path.read_text()
        try:
            data = yaml.load(text, _Loader)  # noqa: S506 - _Loader derives from SafeLoader
        except yaml.YAMLError as error:
            if re.search(r"^services:", text, re.MULTILINE):
                raise Unreadable(
                    f"{path.relative_to(root)}: has services: but cannot be parsed as YAML "
                    f"({error.__class__.__name__}), so what it builds cannot be checked"
                ) from error
            continue
        if isinstance(data, dict) and isinstance(data.get("services"), dict):
            documents.append((path, data))
    return documents


def _service_dockerfile(compose_path: Path, build, root: Path) -> str | None:
    """The repository-relative Dockerfile a compose service builds, if it builds one."""
    if isinstance(build, str):
        context, dockerfile = build, "Dockerfile"
    elif isinstance(build, dict):
        context = build.get("context", ".")
        dockerfile = build.get("dockerfile", "Dockerfile")
    else:
        return None
    if not isinstance(context, str) or not isinstance(dockerfile, str):
        return None
    resolved = (compose_path.parent / context / dockerfile).resolve()
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return None  # outside the repository: not ours to check


def _mounts_the_tree_over_the_baked_copy(compose_path: Path, service: dict, root: Path) -> bool:
    """Does `./shared` cover the baked copy? Then what runs is the tree, never the image."""
    for volume in service.get("volumes") or []:
        if isinstance(volume, dict):
            source, target = volume.get("source"), volume.get("target")
        elif isinstance(volume, str):
            parts = volume.split(":")
            if len(parts) < SOURCE_AND_DESTINATION:
                continue
            source, target = parts[0], parts[1]
        else:
            continue
        if target != SHARED_MOUNT_TARGET or not isinstance(source, str):
            continue
        if (compose_path.parent / source).resolve() == root / SHARED_TREE:
            return True
    return False


def _passes_source_hash(build) -> bool:
    args = build.get("args") if isinstance(build, dict) else None
    if isinstance(args, dict):
        return BUILD_ARG in args
    if isinstance(args, list):
        return any(str(arg).split("=")[0].strip() == BUILD_ARG for arg in args)
    return False


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
        raise Unreadable(f"Makefile has no {target} recipe to read image names from")
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
            raise Unreadable(f"cannot read image name out of: {command}")
        images.append(TrackedImage(tags[0], dockerfile.group(1), f"make {WORKER_IMAGES_RECIPE}"))
    return images


def compose_plan(root: Path = REPO_ROOT) -> tuple[list[str], list[TrackedImage]]:
    """What every compose service that bakes `shared` owes, and the images to compare.

    The two rules are static — no docker is asked anything here. A service that breaks one
    of them lands in the problems; one that keeps them and does not mount the tree over
    its baked copy lands in the tracked images.
    """
    problems: list[str] = []
    images: dict[str, TrackedImage] = {}
    bakers = set(dockerfiles_baking_shared(root))

    for path, data in _compose_documents(root):
        where_file = path.relative_to(root)
        for name, service in data["services"].items():
            if not isinstance(service, dict):
                continue
            build = service.get("build")
            dockerfile = _service_dockerfile(path, build, root)
            if dockerfile is None or dockerfile not in bakers:
                continue
            where = f"{where_file}: service {name} builds {dockerfile}, which bakes shared,"
            if not _passes_source_hash(build):
                problems.append(f"{where} without passing {BUILD_ARG} in build.args")
            reference = service.get("image")
            if not reference:
                problems.append(
                    f"{where} without declaring an image: name — without one the image is "
                    "named after the compose project and cannot be found again"
                )
                continue
            if _mounts_the_tree_over_the_baked_copy(path, service, root):
                continue  # the mount covers the baked copy: what runs is the tree, always
            if ":" not in reference.rsplit("/", 1)[-1]:
                reference = f"{reference}:latest"
            images.setdefault(
                reference, TrackedImage(reference, dockerfile, f"{where_file} service {name}")
            )
    return problems, sorted(images.values(), key=lambda image: image.reference)


def tracked_images(root: Path = REPO_ROOT) -> list[TrackedImage]:
    """Every built image whose baked `shared` is what actually runs."""
    return worker_base_images(root) + compose_plan(root)[1]


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


def static_problems(root: Path = REPO_ROOT) -> tuple[list[str], list[TrackedImage]]:
    """Coverage, before any image is inspected: what the tree itself already says."""
    problems = [f"{name}: {reason}" for name, reason in uncovered_dockerfiles(root)]
    compose_broken, images = compose_plan(root)
    return problems + compose_broken, worker_base_images(root) + images


def check(
    root: Path = REPO_ROOT,
    inspect=docker_image_labels,
    report=print,
) -> list[str]:
    """Every reason the built stand is behind the tree, empty when it is not."""
    try:
        problems, images = static_problems(root)
    except Unreadable as error:
        return [str(error)]
    if problems:
        return problems

    expected = source_hash(root)
    for image in images:
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
