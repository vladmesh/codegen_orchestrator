"""A Python service image carries exactly its lock, and the lock satisfies its pyproject.

Every Python service image installs its third-party dependencies from
``services/<svc>/requirements.lock``. Two things can still go wrong, and this module
names both:

* **Drift.** The image's installed distribution set differs from the lock: a package the
  lock does not pin, a pinned package that is missing, or one installed at another version.
  CI tested the lock, so an image that drifted from it ships versions nobody tested.
* **A stale lock.** ``pyproject.toml`` changed and the lock was not recompiled
  (``make lock-deps``), so the lock no longer satisfies it. ``pip install -r`` installs
  the lock without reading pyproject, so nothing else notices until an import fails.

The comparison is pure. ``PROBE`` is the only part that runs inside an image: it prints
the image's installed distributions, with their requirements, and its PEP 508 marker
environment, and ``scripts/check_service_image_imports.py`` runs it in the image it has
just built, so the check costs no build of its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
import tomllib

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

# Installer tooling the base image ships and no lock pins. Nothing else is ignored,
# except the service's own package (see ``drift``).
TOOLING = frozenset({"pip", "setuptools", "wheel"})

LOCK = "requirements.lock"
PYPROJECT = "pyproject.toml"

# Standard library only: it runs on the image's interpreter, before anything is known
# about what that image has installed.
PROBE = """
import importlib.metadata, json, os, platform, sys

def full_version(info):
    version = f"{info.major}.{info.minor}.{info.micro}"
    if info.releaselevel != "final":
        version += info.releaselevel[0] + str(info.serial)
    return version

print(json.dumps({
    "distributions": [
        {
            "name": dist.metadata["Name"],
            "version": dist.version,
            "requires": dist.requires or [],
        }
        for dist in importlib.metadata.distributions()
    ],
    "environment": {
        "implementation_name": sys.implementation.name,
        "implementation_version": full_version(sys.implementation.version),
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    },
}))
"""


@dataclass(frozen=True)
class Distribution:
    """One installed (or locked) distribution, under its canonical name."""

    name: str
    version: str
    requires: tuple[str, ...] = ()


def parse_lock(text: str, environment: Mapping[str, str]) -> dict[str, str]:
    """``{canonical name: version}`` of every pin in a ``uv pip compile`` lock.

    A pin whose marker does not hold in ``environment`` is not installed there, so it is
    left out. A line that is not an exact ``name==version`` pin raises: a lock this
    cannot read is a hole in the check, never a line to skip.
    """
    pins: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as error:
            raise ValueError(f"line {number} of the lock is not a requirement: {raw!r}") from error
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError(f"line {number} of the lock is not an exact pin: {raw!r}")
        if requirement.marker and not requirement.marker.evaluate(dict(environment)):
            continue
        name = canonicalize_name(requirement.name)
        if name in pins:
            raise ValueError(f"line {number} of the lock pins {name} a second time")
        pins[name] = specifiers[0].version
    return pins


def locked_distributions(pins: Mapping[str, str]) -> dict[str, Distribution]:
    """The lock alone, as distributions whose own requirements are unknown."""
    return {name: Distribution(name, version) for name, version in pins.items()}


def installed_distributions(
    image: str, entries: Iterable[Mapping[str, object]]
) -> tuple[dict[str, Distribution], list[str]]:
    """The probe's distributions by canonical name, and any installed twice.

    The same distribution found twice on ``sys.path`` at one version is one install; at
    two versions the image carries both, which is drift on its own.
    """
    found: dict[str, Distribution] = {}
    problems = []
    for entry in entries:
        name = canonicalize_name(str(entry["name"]))
        requires = tuple(str(requirement) for requirement in entry["requires"])
        dist = Distribution(name, str(entry["version"]), requires)
        previous = found.get(name)
        if previous is not None and Version(previous.version) != Version(dist.version):
            problems.append(
                f"{image}: {name} is installed twice, at {previous.version} and {dist.version}"
            )
            continue
        found[name] = dist
    return found, problems


def drift(
    image: str,
    pins: Mapping[str, str],
    installed: Mapping[str, Distribution],
    own_package: str,
) -> list[str]:
    """Every difference between the image's installed set and its lock.

    Only ``TOOLING`` and the service's own package are ignored on the installed side.
    """
    ignored = TOOLING | {canonicalize_name(own_package)}
    actual = {name: dist.version for name, dist in installed.items() if name not in ignored}
    problems = []
    for name in sorted(actual.keys() | pins.keys()):
        locked = pins.get(name)
        version = actual.get(name)
        if locked is None:
            problems.append(f"{image}: {name} {version} is installed, but {LOCK} does not pin it")
        elif version is None:
            problems.append(f"{image}: {LOCK} pins {name} {locked}, but it is not installed")
        elif Version(version) != Version(locked):
            problems.append(f"{image}: {name} is installed at {version}, but {LOCK} pins {locked}")
    return problems


def unsatisfied(
    image: str,
    requirements: Iterable[str],
    distributions: Mapping[str, Distribution],
    environment: Mapping[str, str],
) -> list[str]:
    """Every requirement of ``pyproject.toml`` the distributions do not satisfy.

    Walks from the pyproject's own requirements through each distribution's requirements,
    extras included, as far as the distributions record them: the image's metadata records
    all of them, a bare lock records none, so over a lock only the direct ones are checked.
    """
    problems = []
    seen: set[tuple[str, frozenset[str]]] = set()

    def applies(requirement: Requirement, extras: frozenset[str]) -> bool:
        if requirement.marker is None:
            return True
        return any(
            requirement.marker.evaluate({**environment, "extra": extra})
            for extra in ("", *sorted(extras))
        )

    def visit(requirement: Requirement, via: str) -> None:
        name = canonicalize_name(requirement.name)
        dist = distributions.get(name)
        if dist is None:
            problems.append(f"{image}: {via} requires {requirement}, which {LOCK} does not pin")
            return
        if not requirement.specifier.contains(dist.version, prereleases=True):
            problems.append(
                f"{image}: {via} requires {requirement}, but {LOCK} pins {name} {dist.version}"
            )
            return
        extras = frozenset(canonicalize_name(extra) for extra in requirement.extras)
        if (name, extras) in seen:
            return
        seen.add((name, extras))
        for raw in dist.requires:
            child = Requirement(raw)
            if applies(child, extras):
                visit(child, f"{name} {dist.version}")

    for raw in requirements:
        requirement = Requirement(raw)
        if applies(requirement, frozenset()):
            visit(requirement, PYPROJECT)
    return sorted(set(problems))


def pyproject_project(text: str) -> tuple[str, list[str]]:
    """The ``[project]`` name and dependencies of a service's pyproject."""
    project = tomllib.loads(text)["project"]
    return project["name"], list(project["dependencies"])


def check_lock_against_pyproject(
    image: str, lock_text: str, pyproject_text: str, environment: Mapping[str, str] | None = None
) -> list[str]:
    """The cheap freshness check: the lock alone pins every direct pyproject requirement."""
    environment = dict(environment or default_environment())
    pins = parse_lock(lock_text, environment)
    _name, requirements = pyproject_project(pyproject_text)
    return unsatisfied(image, requirements, locked_distributions(pins), environment)


def check_image(image: str, lock_text: str, pyproject_text: str, probe_output: str) -> list[str]:
    """Every drift and every unsatisfied pyproject requirement of one built image."""
    probe = json.loads(probe_output)
    environment = probe["environment"]
    installed, problems = installed_distributions(image, probe["distributions"])
    own_package, requirements = pyproject_project(pyproject_text)
    pins = parse_lock(lock_text, environment)
    problems += drift(image, pins, installed, own_package)
    problems += unsatisfied(image, requirements, installed, environment)
    return problems
