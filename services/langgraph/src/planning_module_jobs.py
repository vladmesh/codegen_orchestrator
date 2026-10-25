"""Read-only job facts from the same pinned closure mechanical installation resolves."""

from copy import deepcopy
import hashlib
import json

from framework.spec.loader import _package_prefix
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from shared.contracts.acceptance import parse_scheduled_behaviours
from shared.contracts.dto.catalog_install import CatalogInstall
from shared.contracts.dto.task import TaskDTO, TaskStatus

from . import catalog_install
from .catalog_install import InstallRefusal, resolve_install_payload
from .kit_catalog import KitCatalog, restore_install_snapshot


class PlanningModuleFacts:
    """One attempt's native resolutions, detached from mutable injected state.

    Snapshot bytes and all parsed catalog fields identify the facts, together with
    Python and host tooling. Keep only the latest catalog; a changed input must pass
    native parsing, binding/manifest identity and schema validation before reuse.
    Returned closures and job dictionaries cannot mutate the verified originals.
    """

    def __init__(self) -> None:
        self._restored: tuple[dict, KitCatalog] | None = None
        self._catalog: KitCatalog | None = None
        self._tooling: str | None = None
        self._resolved: dict[tuple[str, str], tuple[CatalogInstall, dict[str, dict]]] = {}

    def restore(self, snapshot: dict) -> KitCatalog:
        if self._restored is None or self._restored[0] != snapshot:
            catalog = restore_install_snapshot(snapshot)
            self._restored = (deepcopy(snapshot), deepcopy(catalog))
        return deepcopy(self._restored[1])

    def resolve(
        self, catalog: KitCatalog, name: str, python_version: str
    ) -> tuple[CatalogInstall, dict[str, dict]]:
        tooling = catalog_install.installed_tooling_commit()
        if self._catalog != catalog or self._tooling != tooling:
            self._resolved.clear()
            self._catalog = deepcopy(catalog)
            self._tooling = tooling
        key = (name, python_version)
        if key not in self._resolved:
            self._resolved[key] = _resolve_module_install(catalog, name, python_version)
        return deepcopy(self._resolved[key])


def selected_install_closures(tasks: list[TaskDTO]) -> list[CatalogInstall]:
    """Released catalog closures only: a local source or a cancelled attempt selects nothing."""
    return [
        task.install
        for task in tasks
        if isinstance(task.install, CatalogInstall) and task.status is not TaskStatus.CANCELLED
    ]


def module_job_contracts(
    catalog: KitCatalog, installs: list[CatalogInstall], *, facts: PlanningModuleFacts | None = None
) -> dict[str, dict]:
    """Resolve only selected releases; any drift or invalid schema refuses planning."""
    contracts = {}
    facts = facts if facts is not None else PlanningModuleFacts()
    resolved: dict[tuple[str, str], CatalogInstall] = {}
    for install in installs:
        name = install.package.name
        key = (name, install.python_version)
        if key not in resolved:
            current, jobs = plan_module_install(catalog, name, install.python_version, facts=facts)
            _merge_job_contracts(contracts, jobs)
            resolved[key] = current
        # Reuse only this invocation's resolution, comparing every selected closure.
        if resolved[key] != install:
            raise InstallRefusal(f"plan_drift: {name}")
    return contracts


def _merge_job_contracts(contracts: dict[str, dict], jobs: dict[str, dict]) -> None:
    for namespace, contract in jobs.items():
        if namespace in contracts and contracts[namespace]["package"] != contract["package"]:
            raise InstallRefusal(f"module_job_namespace_conflict: {namespace}")
        contracts[namespace] = contract


def plan_module_install(
    catalog: KitCatalog,
    name: str,
    python_version: str,
    *,
    facts: PlanningModuleFacts | None = None,
) -> tuple[CatalogInstall, dict[str, dict]]:
    facts = facts if facts is not None else PlanningModuleFacts()
    return facts.resolve(catalog, name, python_version)


def _resolve_module_install(
    catalog: KitCatalog, name: str, python_version: str
) -> tuple[CatalogInstall, dict[str, dict]]:
    """Resolve a new selection and its job facts through one native manifest read."""
    install, manifest = resolve_install_payload(catalog, name, python_version)
    # This is the installed loader's namespace rule, including packages with no jobs.
    namespace = _package_prefix(manifest.name)
    jobs = {
        f"{namespace}.{local}": schema
        for local, schema in manifest.jobs_schema["properties"].items()
    }
    try:
        for schema in jobs.values():
            Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise InstallRefusal(f"invalid_job_schema: {manifest.name}: {error.message}") from error
    return install, {
        namespace: {
            "package": install.package.model_dump(mode="json"),
            "catalog": install.catalog.model_dump(mode="json"),
            "catalog_digest": install.catalog_digest,
            "tooling_commit": install.tooling_commit,
            "manifest_sha256": hashlib.sha256(
                catalog.manifests[manifest.name].encode()
            ).hexdigest(),
            "jobs": jobs,
            "timers": {
                f"{namespace}.{timer.job}": timer.every_seconds for timer in manifest.timers
            },
        }
    }


def module_jobs_briefing(contracts: dict[str, dict]) -> str:
    if not contracts:
        return ""
    return (
        "\n\nSelected module job contracts (read-only, generated by the pinned kit):\n"
        + json.dumps(contracts, sort_keys=True)
        + "\nPackage jobs and timers are already generated: preserve their ownership and "
        "native delivery. Do not redeclare them in service manifests, modify package code, "
        "or add aliases, bridges or duplicate timers. A package timer runs natively and gets "
        "no QA check of its own. Write a FIRE JOB line for a package job only when a "
        "must-requirement's confirmed behaviour is that job, ending the line with "
        "(requirement <id>); then use these exact runtime names and argument schemas. WITH "
        "supplies all required arguments explicitly, including date-time at; QA does not "
        "inject them. THEN names a product GET route and asserts the confirmed "
        "settings/requirements' observable effect."
    )


def unlinked_fire_job_error(criteria: str, requirement_ids: list[str]) -> str | None:
    """In a brief-backed story every FIRE JOB line ends with `(requirement <id>)` of the brief.

    An empty `requirement_ids` means the story is not brief-backed and nothing is required.
    """
    if not requirement_ids:
        return None
    for line in criteria.splitlines():
        for criterion in parse_scheduled_behaviours(line):
            if any(
                criterion.observable.endswith(f"(requirement {requirement_id})")
                for requirement_id in requirement_ids
            ):
                continue
            return (
                f"FIRE JOB {criterion.name} checks no must-requirement of the confirmed brief: "
                "end the line with (requirement <id>) naming one of "
                f"{', '.join(requirement_ids)}. A job no confirmed requirement asks for, "
                "including a selected package's own timer, is not checked with FIRE JOB; "
                "remove that line."
            )
    return None


def module_criteria_error(criteria: str, contracts: dict[str, dict]) -> str | None:
    # Parse each line through the native grammar so its first-name-wins execution rule
    # cannot hide an invalid later criterion during a repository write.
    for line in criteria.splitlines():
        for criterion in parse_scheduled_behaviours(line):
            namespace, separator, _ = criterion.name.partition(".")
            if not separator or namespace not in contracts:
                continue  # Service-owned jobs are not enumerable at planning time.
            jobs = contracts[namespace]["jobs"]
            if criterion.name not in jobs:
                return (
                    f"Unknown selected-module job {criterion.name}; declared names: "
                    f"{', '.join(sorted(jobs)) or '(none)'}. Use the pinned module contract."
                )
            # Same validator as the generated product jobs controller; no argument injection.
            errors = list(
                Draft202012Validator(jobs[criterion.name]).iter_errors(criterion.arguments)
            )
            if errors:
                return (
                    f"Invalid arguments for {criterion.name}: {errors[0].message}. "
                    "Supply WITH matching the declared schema."
                )
    return None
