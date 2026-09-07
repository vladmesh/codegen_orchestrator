"""Whether the deployed product actually took the kit package route.

`mega-brief-package` exists to drive one product through the kit package path,
and the architect's capability ladder puts a shared service *above* the package
option: choosing it is permitted behaviour. A worker asked for the reminder
capability can therefore hand-write a `reminders.tick` job and a
`GET /reminders?user_ref=…` route, and that product satisfies every criterion
this suite reads off the control plane — the published behaviour parses, the
fire is dispatched, the route answers, central QA passes. On it, the deployed
active-package set is empty, so `run_package_activation_checks` finds no
activation and no package behaviour row is ever written: the path
`codegen-orchestrator-1270` built is never entered, and the run would be green
without a package.

So this variant establishes the package route from the deployment itself,
reading the same generated artifacts central QA reads — the active-package
contract and the generated job registry — with the same parsers. Two facts have
to hold: the product records the `reminders` package as active, and its own
registry attributes the fired behaviour to that package rather than to a
service of its own. Anything else is red with the reason, including an artifact
that could not be read: absence is not a pass, which is the rule this sprint
has applied everywhere else.
"""

from __future__ import annotations

from services.langgraph.src.agents.qa.packages import (
    ACTIVE_PACKAGE_CONTRACT,
    GENERATED_JOB_REGISTRY,
    PackageActivation,
    PackageContractUnreadable,
    parse_active_packages,
    parse_job_owners,
)
from shared.live_harness_cleanup import (
    PACKAGE_CONTRACT_ABSENT_MARKER,
    PACKAGE_CONTRACT_FILE_MARKER,
)

#: The artifacts of the deployment this judgement rests on, in probe order.
PACKAGE_ROUTE_ARTIFACTS = (ACTIVE_PACKAGE_CONTRACT, GENERATED_JOB_REGISTRY)

#: The sentence every failure of this check ends with. What is at stake is not
#: the product — a product built as a shared service may be perfectly good — but
#: what this suite is entitled to claim about the package path afterwards.
NOT_THE_PACKAGE_ROUTE = (
    "The product did not take the package route, so this suite proves nothing about it."
)


def unreadable_package_route(detail: str) -> str:
    """The deployment's package contract could not be established at all."""
    return (
        f"the deployed product's kit package contract could not be read: {detail}. "
        f"{NOT_THE_PACKAGE_ROUTE}"
    )


def package_not_active(package: str, recorded: str) -> str:
    return (
        f"the deployed product's {ACTIVE_PACKAGE_CONTRACT} records {recorded}, not the kit "
        f"package {package!r}. {NOT_THE_PACKAGE_ROUTE}"
    )


def behaviour_not_attributed(package: str, behaviour: str, owner: str) -> str:
    return (
        f"the deployed product's {GENERATED_JOB_REGISTRY} attributes {behaviour!r} to {owner}, "
        f"not to the kit package {package!r}. {NOT_THE_PACKAGE_ROUTE}"
    )


def parse_package_probe(text: str) -> dict[str, str | None]:
    """The probe's answer per artifact: its content, or `None` for "not there".

    A read that failed never reaches here — the probe exits non-zero — so the
    two values in this mapping are the only two answers about the product, and
    neither of them is "the probe could not look".
    """
    artifacts: dict[str, str | None] = {}
    current: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if current is not None:
            artifacts[current] = "\n".join(lines)

    for line in text.splitlines():
        if line.startswith(PACKAGE_CONTRACT_FILE_MARKER):
            flush()
            current = line[len(PACKAGE_CONTRACT_FILE_MARKER) :].strip()
            lines = []
        elif line.startswith(PACKAGE_CONTRACT_ABSENT_MARKER):
            flush()
            current = None
            lines = []
            artifacts[line[len(PACKAGE_CONTRACT_ABSENT_MARKER) :].strip()] = None
        elif current is not None:
            lines.append(line)
    flush()
    return artifacts


def package_route_facts(
    probe: str, *, package: str, behaviour: str
) -> tuple[dict | None, str | None]:
    """Read the deployment's own answer: package facts, or the reason it is red.

    Exactly one of the two is returned. The facts name what was read and where
    it was read from, so a green run says which artifacts of which deployment
    it rests on rather than asserting the package route happened.
    """
    artifacts = parse_package_probe(probe)
    missing = [path for path in PACKAGE_ROUTE_ARTIFACTS if path not in artifacts]
    if missing:
        return None, unreadable_package_route(
            f"the probe of the deployment answered nothing about {', '.join(missing)}"
        )

    contract = artifacts[ACTIVE_PACKAGE_CONTRACT]
    if contract is None:
        return None, unreadable_package_route(
            f"{ACTIVE_PACKAGE_CONTRACT} is not on the deployment, so the product records no "
            "active kit package at all"
        )
    registry = artifacts[GENERATED_JOB_REGISTRY]
    if registry is None:
        return None, unreadable_package_route(
            f"{GENERATED_JOB_REGISTRY} is not on the deployment, so the product attributes no "
            "job to any package"
        )
    try:
        packages = parse_active_packages(contract)
        jobs = parse_job_owners(registry)
    except PackageContractUnreadable as error:
        return None, unreadable_package_route(str(error))

    active = {one.name: one for one in packages}
    if package not in active:
        recorded = ", ".join(sorted(active)) or "no active kit package"
        return None, package_not_active(package, recorded)

    activation = PackageActivation(packages=packages, listed=tuple(active), jobs=jobs)
    declarer = activation.package_jobs.get(behaviour)
    if declarer != package:
        owner = jobs.get(behaviour)
        stated = f"{owner!r}" if owner is not None else "no declarer at all"
        return None, behaviour_not_attributed(package, behaviour, stated)

    one = active[package]
    return {
        "package": one.name,
        "version": one.version,
        "manifest_sha256": one.manifest_sha256,
        "behaviour": behaviour,
        "declared_by": f"package:{package}",
        "read_from": list(PACKAGE_ROUTE_ARTIFACTS),
    }, None
