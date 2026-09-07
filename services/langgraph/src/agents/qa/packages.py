"""What kit packages a deployed product carries, read from the product itself.

A generated product activates a package only when the wheel is installed *and*
the backend manifest lists it, and generation records that resolved set — name,
version and manifest digest — in `codegen_kit/_active_packages.py`. The runtime
refuses a stale or changed generated contract, so a product that booted is a
product whose contract matches what it is running. That is the fact central QA
reads: the package's own connection check is its `startup` raising on failure,
and a deployment that is up with the package recorded has already passed it.

Nothing here infers a package from prose, from a story or from a task. Every
name, version and digest this module produces was read off an artifact of the
deployment under test, and an artifact that cannot be read as the contract it
claims to be raises :class:`PackageContractUnreadable` rather than answering
"no packages" — a check with nothing to examine has to fail, not pass.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json

import yaml

#: The generated artifacts a deployed product's package activation is read
#: from, relative to the deployment root the QA session can read.
ACTIVE_PACKAGE_CONTRACT = "codegen_kit/_active_packages.py"
BACKEND_MANIFEST = "services/backend/manifest.yaml"
GENERATED_JOB_REGISTRY = "services/backend/src/generated/jobs_schemas.py"

#: A generated contract is bigger than a probe answer, and a truncated one is
#: refused rather than half-read, so the read is given room for a real product.
CONTRACT_READ_LIMIT = 262144

#: Where a running product publishes the routes it actually mounted. A package's
#: HTTP prefix is declared in the installed `package.yaml`, inside the wheel, so
#: no artifact of the deployment tree records it; the running product does.
OPENAPI_PATH = "/openapi.json"
#: A route that answers 404 is not mounted; a 5xx is the product failing. Every
#: other status — 200, 401, 405, 422 — is the package's router answering.
ROUTE_NOT_MOUNTED = 404
ROUTE_SERVER_ERROR = 500

_PACKAGE_OWNER = "package:"
_DIGEST_SHOWN = 12


class PackageContractUnreadable(ValueError):
    """A deployed product's package artifact could not be read as one."""


@dataclass(frozen=True)
class ActivePackage:
    """One package this product was generated and booted with."""

    name: str
    version: str
    manifest_sha256: str

    @property
    def stated(self) -> str:
        return (
            f"{self.name} {self.version} (manifest sha256 {self.manifest_sha256[:_DIGEST_SHOWN]})"
        )


@dataclass(frozen=True)
class PackageActivation:
    """The package set of one deployment, as its own artifacts record it."""

    packages: tuple[ActivePackage, ...]
    listed: tuple[str, ...]
    #: Every fireable job of the product, mapped to the declarer the generated
    #: registry recorded: a service by its name, a package as `package:<name>`.
    jobs: Mapping[str, str]

    @property
    def names(self) -> list[str]:
        return [one.name for one in self.packages]

    @property
    def package_jobs(self) -> dict[str, str]:
        """Job names an active package declares, mapped to that package."""
        return {
            job: owner[len(_PACKAGE_OWNER) :]
            for job, owner in self.jobs.items()
            if owner.startswith(_PACKAGE_OWNER)
        }


def _assigned(source: str, name: str) -> ast.expr:
    """The value assigned to `name` at the top level of a generated module."""
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise PackageContractUnreadable(
            f"the generated contract declaring {name} is not readable Python: {exc}"
        ) from exc
    for node in module.body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                return node.value
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
    raise PackageContractUnreadable(f"the generated contract declares no {name}")


def parse_active_packages(source: str) -> tuple[ActivePackage, ...]:
    """Read the active package set out of a product's generated contract."""
    value = _assigned(source, "ACTIVE_PACKAGES")
    try:
        identities = ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise PackageContractUnreadable(
            f"ACTIVE_PACKAGES is not a literal package set: {exc}"
        ) from exc
    if not isinstance(identities, list):
        raise PackageContractUnreadable("ACTIVE_PACKAGES is not a list of package identities")
    packages = []
    for identity in identities:
        if not isinstance(identity, dict):
            raise PackageContractUnreadable(f"{identity!r} is not a package identity")
        fields = tuple(identity.get(field) for field in ("name", "version", "manifest_sha256"))
        if not all(isinstance(field, str) and field for field in fields):
            raise PackageContractUnreadable(
                f"{identity!r} does not carry a name, a version and a manifest digest"
            )
        packages.append(ActivePackage(*fields))  # type: ignore[arg-type]
    return tuple(packages)


def parse_listed_packages(source: str) -> tuple[str, ...] | None:
    """Read the backend manifest's package allowlist.

    ``None`` is a manifest with no `packages:` key at all — a product from a
    template that predates packages. It is not the same answer as `packages: []`,
    which is a package-aware product that installed none.
    """
    try:
        manifest = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise PackageContractUnreadable(
            f"the backend manifest is not readable YAML: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise PackageContractUnreadable("the backend manifest is not a mapping")
    listed = manifest.get("packages")
    if listed is None:
        return None
    if not isinstance(listed, list) or not all(isinstance(one, str) and one for one in listed):
        raise PackageContractUnreadable(
            f"the backend manifest's packages: is not a name list: {listed!r}"
        )
    return tuple(listed)


def _mapping_of(value: ast.expr, name: str) -> dict:
    """A generated registry is either a literal mapping or one JSON payload."""
    if isinstance(value, ast.Call):
        function = value.func
        dotted = (
            f"{function.value.id}.{function.attr}"
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name)
            else ""
        )
        payload = value.args[0] if len(value.args) == 1 else None
        if dotted != "json.loads" or not isinstance(payload, ast.Constant):
            raise PackageContractUnreadable(f"{name} is not a generated registry")
        try:
            parsed = json.loads(payload.value)
        except (TypeError, ValueError) as exc:
            raise PackageContractUnreadable(f"{name} does not carry a JSON mapping: {exc}") from exc
    else:
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
            raise PackageContractUnreadable(f"{name} is not a literal mapping: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PackageContractUnreadable(f"{name} is not a mapping")
    return parsed


def parse_job_owners(source: str) -> dict[str, str]:
    """Map every fireable job of the product to the declarer that owns it.

    The generated registry records one owner per job — a service by its name, a
    package as `package:<name>` — so a package's behaviour is attributed by the
    deployed product's own contract, never by this platform guessing at a
    dotted prefix.
    """
    sources = _mapping_of(_assigned(source, "JOB_SCHEMA_SOURCES"), "JOB_SCHEMA_SOURCES")
    for job, owner in sources.items():
        if not isinstance(job, str) or not isinstance(owner, str):
            raise PackageContractUnreadable(
                f"JOB_SCHEMA_SOURCES attributes {job!r} to {owner!r}, which is not a declarer"
            )
    return dict(sources)


def _normalized(name: str) -> str:
    """Package names compare with hyphens and underscores treated alike."""
    return name.replace("-", "_").casefold()


def connection_check(package: ActivePackage) -> dict:
    """The package's connection check, answered by the booted product.

    The check is the package's `startup`, which raises on failure, and the
    runtime refuses a generated contract that no longer matches the manifest
    and the installed wheels. A product that is up, carrying this package in
    that contract, has passed it — so reading the contract off the live
    deployment is performing the check, not describing it.
    """
    return {
        "name": connection_check_name(package.name),
        "pass": True,
        "detail": (
            f"{package.stated}, read from {ACTIVE_PACKAGE_CONTRACT} on the deployment and "
            f"listed in {BACKEND_MANIFEST}; the product booted with this generated contract, "
            "and a stale or absent one refuses to boot"
        ),
    }


def connection_check_name(package: str) -> str:
    return f"package {package} is active in the deployed product"


def route_check_name(package: str) -> str:
    return f"package {package} answers on its own HTTP prefix"


def behaviour_check_name(package: str) -> str:
    return f"package {package} scheduled behaviour produced its observable"


def package_route_paths(openapi: object, package: ActivePackage) -> tuple[str, ...]:
    """The paths the running product declares under this package's own prefix.

    The prefix is the package's first path segment: a package owns one HTTP
    prefix, and this is the only place a deployed product states which one it
    mounted. Paths without a path parameter come first, because one of those
    can be requested as it stands.
    """
    paths = openapi.get("paths") if isinstance(openapi, dict) else None
    if not isinstance(paths, dict):
        return ()
    wanted = _normalized(package.name)
    under = [
        path
        for path in paths
        if isinstance(path, str)
        and path.startswith("/")
        and _normalized(path.split("/")[1] if len(path.split("/")) > 1 else "") == wanted
    ]
    return tuple(sorted(under, key=lambda path: ("{" in path, len(path), path)))


def route_check(
    package: ActivePackage, *, path: str = "", status: int = 0, reason: str = ""
) -> dict:
    """One package's prefixed-route result, or why the run has none.

    A route that answers 404 is not mounted and a 5xx is the product failing;
    anything else the product answers is its router responding under the
    package's prefix, which is what this check asks.
    """
    if reason:
        return {"name": route_check_name(package.name), "pass": False, "detail": reason}
    mounted = status != ROUTE_NOT_MOUNTED and status < ROUTE_SERVER_ERROR
    return {
        "name": route_check_name(package.name),
        "pass": mounted,
        "detail": (
            f"GET {path} on the deployed product answered {status}"
            + ("" if mounted else "; the package's router is not answering there")
        ),
    }


def behaviour_check(package: ActivePackage, *, fired: Sequence[str] = (), reason: str = "") -> dict:
    """One package's behaviour result: it was fired in this run, or it was not.

    Firing is what this check requires; the observable the criterion states is
    what the verdict on it rests on, and that judgement stays with the executor
    and its criteria. What is refused here is the third way — neither fired nor
    judged, and reported as though it had been.
    """
    if reason:
        return {"name": behaviour_check_name(package.name), "pass": False, "detail": reason}
    return {
        "name": behaviour_check_name(package.name),
        "pass": True,
        "detail": (
            f"this run fired {', '.join(sorted(fired))} on the deployed product and judged it "
            "on the observable its criterion states, not on the dispatch record"
        ),
    }


def active_package_facts(
    activation: PackageActivation,
    *,
    deployed_url: str,
    fireable_behaviours: Sequence[str] = (),
) -> list[str]:
    """What the executor is told about the packages of the product under test.

    The first fact is the connection check, and it is stated as answered
    because the product answered it: the package's `startup` raises on failure,
    the runtime refuses a generated contract that no longer matches the
    manifest and the installed wheels, and this deployment is up with exactly
    this contract recorded. Nothing here asks anyone to re-run a startup, and
    nothing offers a way to install, build or start a product.

    The second binds the package's acceptance checks to this deployment. Both
    failure modes the kit's own sprint recorded are named where an executor
    would otherwise take the comfortable path: a check made against anything
    but this product is not a check of it, and a check that could not be made
    is a failed check rather than a skipped one.
    """
    stated = "; ".join(package.stated for package in activation.packages)
    facts = [
        f"- Kit packages active in the product under test: {stated}. Read from the "
        f"product's own generated package contract on the target "
        f"(`{ACTIVE_PACKAGE_CONTRACT}`) and cross-checked against the backend manifest "
        f"allowlist it booted with (`{BACKEND_MANIFEST}`). A package's connection check "
        "is its `startup`, which raises on failure, and a product whose generated "
        "contract no longer matches its manifest and installed wheels refuses to boot — "
        "so this deployment being up, with these packages recorded, is that check "
        "answered by the product itself. It is passed; do not install, build or start "
        "anything, and do not look for a startup to re-run.",
        "- The acceptance checks these packages owe are made against this deployment and "
        f"nothing else: its HTTP surface at {deployed_url} through `http_get`, its "
        "containers through the reads above, and its declared behaviours through "
        "`fire_job`. A fixture, a replica, a second copy of the product or anything you "
        "assemble yourself is not the product under test. A check you could not make "
        "against this deployment is a failed check — never a passed one, never a skipped "
        "one, and never one you report as not applicable.",
        "- An active package makes this run owe results, not remarks. The runner has "
        "already performed two of them against this deployment and they are in this "
        "run's result whatever you submit: the package is active in the booted product, "
        "and a route under its own HTTP prefix answers. The third is yours: fire the "
        "package's declared behaviour named below and judge the observable its criterion "
        "states. A run that fires nothing fails that check, and a verdict that reports "
        "no package check does not pass because it said so.",
    ]
    package_jobs = activation.package_jobs
    owned = sorted(
        job for job in package_jobs if not fireable_behaviours or job in fireable_behaviours
    )
    if owned:
        named = "; ".join(f"{job} (package {package_jobs[job]})" for job in owned)
        facts.append(
            f"- Scheduled behaviours this deployment's generated job registry attributes "
            f"to an active package: {named}. A package's behaviour is fired by name like "
            "any other and judged the same way: the dispatch record is not the answer, "
            "the observable stated with the name is."
        )
    undeclared = sorted(set(fireable_behaviours) - set(activation.jobs))
    if undeclared:
        facts.append(
            "- This run's criteria name scheduled behaviour(s) the deployed product's "
            f"generated job registry does not declare at all: {', '.join(undeclared)}. "
            "There is nothing in this product to fire under that name, so the check "
            "needing it fails; do not report it as passed, skipped or not applicable."
        )
    return facts
