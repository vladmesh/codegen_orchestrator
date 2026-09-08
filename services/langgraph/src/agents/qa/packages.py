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
import re

import yaml

from shared.generated_contracts import (
    ACTIVE_PACKAGE_CONTRACT,
    BACKEND_MANIFEST,
    GENERATED_CONTRACT_READ_LIMIT,
    GENERATED_JOB_REGISTRY as GENERATED_JOB_REGISTRY,
)

CONTRACT_READ_LIMIT = GENERATED_CONTRACT_READ_LIMIT


_PACKAGE_OWNER = "package:"
_DIGEST_SHOWN = 12
#: A path a criterion's observable names, so the run is told which read of the
#: product answers it instead of the platform guessing at one.
_OBSERVABLE_PATH = re.compile(r"(?<![\w/])/[A-Za-z0-9_][A-Za-z0-9_\-./]*")
#: A token shorter than this matches too much to bind anything.
_MIN_TOKEN = 3


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


def observable_paths(observable: str) -> tuple[str, ...]:
    """The product routes a criterion's observable names, if it names any.

    An observable that says where to look — "GET /reminders?user_ref=42 shows
    the reminder as emitted" — tells the run which read answers it, and the
    platform requires that read rather than accepting an unrelated one. An
    observable that names no route leaves the run to read what it can, and the
    result says so instead of pretending the read was narrowed.
    """
    return tuple(
        dict.fromkeys(
            match.group(0).rstrip(".,;") for match in _OBSERVABLE_PATH.finditer(observable)
        )
    )


def observation_tokens(tool: str, subject: str) -> tuple[str, ...]:
    """What a check may quote to show it rests on this read.

    The tokens are the words the request itself used, so a check that says what
    it did contains one of them and a check that says nothing does not.
    """
    tokens = {subject}
    if subject.startswith("/"):
        tokens.add(subject.split("?")[0])
    if subject.startswith("@"):
        tokens.add(subject[1:])
        tokens.add("telegram")
    if tool == "remote_exec":
        tokens.update(part for part in subject.split() if len(part) >= _MIN_TOKEN)
    return tuple(sorted(token for token in tokens if len(token) >= _MIN_TOKEN))


def observation_answers(observable: str, tool: str, subject: str) -> bool:
    """Whether this read is one the criterion's observable asked for.

    Only a read of a route the observable names answers it. An observable that
    names no route names nothing this run can read, so nothing can be bound to
    it and no read answers it — the behaviour row then fails saying so, rather
    than passing on an unrelated read. That is the same rule everywhere else in
    this path: a check that examined nothing, or examined the wrong thing, is
    not a pass.
    """
    paths = observable_paths(observable)
    if not paths or tool not in {"http_get", "localhost_http_get"}:
        return False
    read = subject.split("?")[0]
    return any(read == path or read.startswith(f"{path.rstrip('/')}/") for path in paths)


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


def behaviour_check_name(package: str, behaviour: str = "") -> str:
    """One row per declared behaviour, so none of them can go missing."""
    if behaviour:
        return f"package {package} behaviour {behaviour} produced its observable"
    return f"package {package} declared behaviour produced its observable"


def behaviour_check(
    package: ActivePackage,
    *,
    behaviour: str = "",
    observable: str = "",
    observed: str = "",
    judged: str = "",
    reason: str = "",
) -> dict:
    """One declared behaviour's result, and what the platform may say about it.

    Not the fire, and not the fire's receipt: the product answers both with the
    core's record of the dispatch, and this platform's own contract says that
    record is not evidence anything consumed the event or ran the behaviour.
    What this row requires is that the run fired the behaviour, then read the
    product's own output, and reported a check resting on that read.

    What it establishes is said plainly, in the row itself: the work was done
    and the executor judged it. Not that the criterion's prose observable is
    mechanically proven — the observable is English an architect wrote, no
    runner-side rule reads English, and a row claiming otherwise would be the
    fault this sprint has corrected five times.
    """
    if reason:
        return {
            "name": behaviour_check_name(package.name, behaviour),
            "pass": False,
            "detail": reason,
        }
    return {
        "name": behaviour_check_name(package.name, behaviour),
        "pass": True,
        "detail": (
            f"this run fired {behaviour} on the deployed product, then read the product's own "
            f"output with {observed}, and its check {judged!r} rests on that read and passed. "
            f"The criterion's observable: {observable}. The platform establishes that the work "
            "was done and that the executor judged it; the words of the observable are the "
            "executor's judgement, not a mechanical proof"
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
        "- An active package makes this run owe results, not remarks. One the runner has "
        "already performed against this deployment, and it is in this run's result "
        "whatever you submit: the package is active in the booted product. The others are "
        "yours, one for each package behaviour named below, and none of them is finished "
        "by firing. For each: fire it, then read the route its criterion's observable "
        "names, and report a check that names the behaviour and quotes the exact request "
        "you made, so the result shows what it rests on. A dispatch record is not that "
        "read and neither is `job_evidence`: both answer with the product core's record of "
        "the fire. A behaviour that was not fired, whose named route was not read, or "
        "whose check rests on no read you made, fails — one row each, so none of them can "
        "go missing. A criterion whose observable names no route to read cannot be bound "
        "to one at all, and its row fails on that; report it as failed with that reason "
        "rather than looking for something else to pass it on.",
        "- Where a package's routes are mounted, this deployment does not say: package "
        "protocol v1 keeps the HTTP prefix in the installed package.yaml, inside the "
        "wheel, and the generated contract records only name, version and manifest "
        "digest. So no prefixed-route check is required of this run and none is inferred "
        "from the package's name. If this run's criteria name a route of the package, "
        "check it as the ordinary criterion it is.",
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
