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
import re
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

# A direct reference pinned as exactly as ``==`` pins a release: a VCS URL at a full commit.
# The lock records it by that URL, because the version is whatever the commit builds.
_COMMIT_PIN = re.compile(r"git\+\S+@[0-9a-f]{40}")


def is_commit_pin(pin: str) -> bool:
    """Whether a lock pin is a VCS URL at a full commit rather than a release version."""
    return _COMMIT_PIN.fullmatch(pin) is not None


# Standard library only: it runs on the image's interpreter, before anything is known
# about what that image has installed.
PROBE = """
import importlib.metadata, json, os, platform, sys

def direct_url(dist):
    # PEP 610: how a distribution installed from a URL or VCS got there, if it did.
    raw = dist.read_text("direct_url.json")
    return json.loads(raw) if raw else None

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
            "direct_url": direct_url(dist),
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
    #: Where a VCS install came from, as ``<vcs>+<url>@<commit>`` (the shape of a lock's
    #: commit pin), read from its PEP 610 ``direct_url.json``; ``None`` for anything else.
    source: str | None = None


def vcs_source(direct_url: object) -> str | None:
    """``<vcs>+<url>@<commit>`` of a PEP 610 VCS ``direct_url.json``, else ``None``."""
    if not isinstance(direct_url, Mapping):
        return None
    vcs_info = direct_url.get("vcs_info")
    if not isinstance(vcs_info, Mapping):
        return None
    vcs, url, commit = vcs_info.get("vcs"), direct_url.get("url"), vcs_info.get("commit_id")
    if not (isinstance(vcs, str) and isinstance(url, str) and isinstance(commit, str)):
        return None
    return f"{vcs}+{url}@{commit}"


def parse_lock(text: str, environment: Mapping[str, str]) -> dict[str, str]:
    """``{canonical name: version}`` of every pin in a ``uv pip compile`` lock.

    A ``name @ git+<url>@<commit>`` pin maps to its URL instead (``is_commit_pin``). A pin
    whose marker does not hold in ``environment`` is not installed there, so it is left
    out. A line that is neither an exact ``name==version`` pin nor a VCS pin at a full
    commit raises: a lock this cannot read is a hole in the check, never a line to skip.
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
        if requirement.url is not None:
            if not is_commit_pin(requirement.url):
                raise ValueError(f"line {number} of the lock is not an exact pin: {raw!r}")
            pin = requirement.url
        elif len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError(f"line {number} of the lock is not an exact pin: {raw!r}")
        else:
            pin = specifiers[0].version
        if requirement.marker and not requirement.marker.evaluate(dict(environment)):
            continue
        name = canonicalize_name(requirement.name)
        if name in pins:
            raise ValueError(f"line {number} of the lock pins {name} a second time")
        pins[name] = pin
    return pins


def locked_distributions(pins: Mapping[str, str]) -> dict[str, Distribution]:
    """The lock alone, as distributions whose own requirements are unknown.

    A commit pin is its own source: the lock names the commit, not the version it builds.
    """
    return {
        name: Distribution(name, pin, source=pin if is_commit_pin(pin) else None)
        for name, pin in pins.items()
    }


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
        source = vcs_source(entry.get("direct_url"))
        dist = Distribution(name, str(entry["version"]), requires, source)
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

    Only ``TOOLING`` and the service's own package are ignored on the installed side. A
    commit pin is compared by provenance: the installed distribution must record, in its
    PEP 610 ``direct_url.json``, a VCS install of the same repository URL at the same
    commit. Missing provenance, another URL or another commit is drift.
    """
    ignored = TOOLING | {canonicalize_name(own_package)}
    actual = {name: dist for name, dist in installed.items() if name not in ignored}
    problems = []
    for name in sorted(actual.keys() | pins.keys()):
        locked = pins.get(name)
        dist = actual.get(name)
        version = None if dist is None else dist.version
        if locked is None:
            problems.append(f"{image}: {name} {version} is installed, but {LOCK} does not pin it")
        elif dist is None:
            problems.append(f"{image}: {LOCK} pins {name} {locked}, but it is not installed")
        elif is_commit_pin(locked):
            if dist.source is None:
                problems.append(
                    f"{image}: {name} {version} is installed without VCS provenance, "
                    f"but {LOCK} pins {locked}"
                )
            elif dist.source != locked:
                problems.append(
                    f"{image}: {name} is installed from {dist.source}, but {LOCK} pins {locked}"
                )
        elif Version(version) != Version(locked):
            problems.append(f"{image}: {name} is installed at {version}, but {LOCK} pins {locked}")
    return problems


def _satisfies(requirement: Requirement, dist: Distribution) -> bool:
    """Whether one distribution, locked or installed, satisfies one requirement.

    A URL requirement is satisfied only by a distribution from that same source: a commit
    pin of that URL, or an install whose provenance records it. A lock's commit pin carries
    no version, so it satisfies no version range.
    """
    if requirement.url:
        return dist.source == requirement.url
    if is_commit_pin(dist.version):
        return not requirement.specifier
    return requirement.specifier.contains(dist.version, prereleases=True)


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
        if not _satisfies(requirement, dist):
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
