#!/usr/bin/env python3
"""The buildx layer cache CI builds through: GitHub Actions cache, one scope per Dockerfile.

One main push builds about fifteen distinct images, most of them in several jobs: the
API image alone is built by two service legs, four integration legs, the DinD suite and
the service image import check. A scope per job would give each of those builds a cache
of its own that only its next run of the same job reads. So the scope is the image
definition, the Dockerfile, and every build of it in any job reads and writes one cache:
``api:test`` in the scheduler leg hits the layers the api leg wrote a minute earlier.

Two callers wire it in:

* the test jobs (``compose-override``): every service a test compose file builds gets
  ``cache_from``/``cache_to`` in a compose override that ``make`` merges through
  ``TEST_COMPOSE_OVERRIDE``;
* ``scripts/check_service_image_imports.py --layer-cache gha``, whose bake targets carry
  the same entries.

The cache is written with ``mode=max``: most service Dockerfiles have a builder stage
whose dependency layers are the slow part, and ``mode=min`` would drop them. Writing is
``ignore-error=true``: a throttled or unavailable cache service costs speed, never a
red build. Reading a missing scope is a cache miss, not an error. The repository cache
holds 10 GB and evicts the least recently used entries past that; the layers are
content-addressed, so the base image and apt layers the Dockerfiles share are stored
once, and a pull request uploads only the layers its change produced (docs/TESTING.md,
"Docker layer cache").

The ``gha`` backend needs the Actions runtime token, which GitHub hands to actions and
not to ``run:`` steps; ci.yml exposes it with crazy-max/ghaction-github-runtime first.
Standard library only: the test jobs run it on the runner's own python3.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "gha"
SCOPE_PREFIX = "buildx-"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def scope(dockerfile: str) -> str:
    """The cache scope of one image definition: its Dockerfile's repository path."""
    path = PurePosixPath(dockerfile)
    if path.is_absolute() or ".." in path.parts or not path.name:
        raise ValueError(f"{dockerfile!r} is not a Dockerfile path inside the repository")
    return SCOPE_PREFIX + _UNSAFE.sub("_", str(path))


def cache_from(dockerfile: str) -> list[str]:
    return [f"type={BACKEND},scope={scope(dockerfile)}"]


def cache_to(dockerfile: str) -> list[str]:
    return [f"type={BACKEND},scope={scope(dockerfile)},mode=max,ignore-error=true"]


def compose_override(config: dict[str, Any], root: Path = ROOT) -> dict[str, Any]:
    """A compose override that caches every service the resolved compose file builds.

    ``config`` is ``docker compose config --format json`` of one file: its build
    contexts are absolute and a build's dockerfile is relative to its context, so the
    scope is read off the Dockerfile's path in the repository, whatever the context.
    """
    services = config.get("services")
    if not isinstance(services, dict):
        raise ValueError("the compose config has no services mapping")
    override: dict[str, Any] = {}
    for name, service in sorted(services.items()):
        build = service.get("build") if isinstance(service, dict) else None
        if build is None:
            continue
        dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
        context = build.get("context") if isinstance(build, dict) else None
        if not isinstance(dockerfile, str) or not dockerfile or not isinstance(context, str):
            raise ValueError(f"service {name} builds without a named context and dockerfile")
        dockerfile = os.path.relpath(Path(context, dockerfile), root)
        override[name] = {
            "build": {"cache_from": cache_from(dockerfile), "cache_to": cache_to(dockerfile)}
        }
    if not override:
        raise ValueError("the compose file builds nothing, so there is no cache to wire")
    return {"services": override}


def resolved_compose(compose_file: str) -> dict[str, Any]:
    output = subprocess.run(
        ["docker", "compose", "-f", compose_file, "config", "--format", "json"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    override = commands.add_parser(
        "compose-override", help="write the cache override of one test compose file"
    )
    override.add_argument("compose_file")
    override.add_argument("output")
    arguments = parser.parse_args(argv)

    document = compose_override(resolved_compose(arguments.compose_file))
    # JSON is YAML, so compose reads the override as written.
    with open(arguments.output, "w", encoding="utf-8") as output:
        json.dump(document, output, indent=2, sort_keys=True)
    for name, service in document["services"].items():
        print(f"{name}: {service['build']['cache_from'][0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
