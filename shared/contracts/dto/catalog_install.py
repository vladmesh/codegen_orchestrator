"""Finite catalog selection and non-engineering operation ownership."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
Version = Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Stage = Literal[
    "queued",
    "claimed",
    "prepare",
    "preflight",
    "package",
    "library",
    "bind",
    "generate",
    "validate",
    "readback",
    "commit",
    "push",
    "published",
    "lease_lost",
    "cancelled",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallComponent(Strict):
    name: Name
    distribution: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")]
    version: Version
    tag: str

    @model_validator(mode="after")
    def released_identity(self):
        if self.tag != f"packages/{self.name}/v{self.version}":
            raise ValueError("component tag must identify its independent released version")
        return self


class DefaultBinding(Strict):
    package: Name
    resource: Annotated[
        str,
        Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*:bindings/[a-z][a-z0-9_-]*\.yaml$"),
    ]
    sha256: Digest
    functions: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_]*$")]]


#: The one kit repository the catalog, its package tags and the kit tooling come from.
KIT_REPOSITORY = "https://github.com/vladmesh/codegen-product-kit.git"
#: Its raw-file base, from which the Architect reads catalog, bindings and manifests.
KIT_RAW_SOURCE = "https://raw.githubusercontent.com/vladmesh/codegen-product-kit"


class CatalogSource(Strict):
    """The exact catalog an install resolves and installs from: never a moving branch.

    `kit add`, the fixed install probe and the planner all read the catalog at `commit`
    of `repository`; `catalog_sha256` is the raw `packages/catalog.yaml` they must find.
    """

    repository: Literal["https://github.com/vladmesh/codegen-product-kit.git"]
    commit: SHA
    catalog_sha256: Digest


class CatalogActivation(Strict):
    """The one activated, verified kit catalog snapshot the Architect plans from.

    Written down once (`shared/catalog_activation.yaml`) and changed only by a reviewed
    orchestrator change; no reader falls back to a live branch. `catalog_digest` is the
    semantic digest of the parsed catalog (`framework.catalog.parse_catalog`), and
    `core_version`/`tooling_commit` the kit host this snapshot was verified against.
    """

    repository: Literal["https://github.com/vladmesh/codegen-product-kit.git"]
    raw_source: Literal["https://raw.githubusercontent.com/vladmesh/codegen-product-kit"]
    commit: SHA
    catalog_sha256: Digest
    catalog_digest: Digest
    core_version: Version
    tooling_commit: SHA

    def source(self) -> CatalogSource:
        return CatalogSource(
            repository=self.repository, commit=self.commit, catalog_sha256=self.catalog_sha256
        )


class CatalogInstall(Strict):
    package: InstallComponent
    libraries: list[InstallComponent] = Field(max_length=16)
    binding: DefaultBinding
    core_version: Version
    python_version: Version
    catalog_digest: Digest
    tooling_commit: SHA
    #: Where `kit add` and the probe read the catalog. Every payload a planner writes now
    #: names it; a stored payload from before it is read, and refused at admission.
    catalog: CatalogSource | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def closure(self):
        names = [self.package.name, *(item.name for item in self.libraries)]
        if len(names) != len(set(names)) or self.binding.package != self.package.name:
            raise ValueError("duplicate component or mismatched binding owner")
        if any(function.split(".")[0] not in names[1:] for function in self.binding.functions):
            raise ValueError("binding requires a library outside the install closure")
        return self


#: The one result version of the kit's read-only `kit check-install --json`.
PREFLIGHT_RESULT_VERSION = 1
#: `kit check-install` exits with the status it prints, and with nothing else.
PREFLIGHT_EXIT_CODES = {"mechanical": 0, "glue": 3, "incompatible": 4}


class PreflightOther(Strict):
    """The other owner a conflict names, as the kit reports it."""

    owner: str
    path: str | None
    line: int | None
    symbol: str | None


class PreflightGlue(Strict):
    """One product-side conflict the kit found, bound to its file and a concrete action."""

    code: str = Field(min_length=1)
    path: str | None
    line: int | None
    owner: str = Field(min_length=1)
    symbol: str | None
    key: str | None
    command: str | None
    conflict: str = Field(min_length=1)
    action: str = Field(min_length=1)
    other: PreflightOther | None


class PreflightTarget(Strict):
    """The exact release the kit read: always the catalog route, never a live default."""

    route: Literal["catalog"]
    catalog_source: str
    catalog_ref: SHA
    tag: str
    version: Version
    requires_core: str
    metadata_sha256: Digest


class PreflightIncompatible(Strict):
    code: str = Field(min_length=1)
    explanation: str


class InstallPreflight(Strict):
    """`kit check-install --json` result version 1, validated as the kit defines it.

    `mechanical` installs as is; `glue` names product-side conflicts; `incompatible`
    names one stable reason. The status, its list and its reason must agree.
    """

    result_version: Annotated[int, Field(ge=PREFLIGHT_RESULT_VERSION, le=PREFLIGHT_RESULT_VERSION)]
    package: Name
    status: Literal["mechanical", "glue", "incompatible"]
    product_core: Version | None
    target: PreflightTarget | None
    glue: list[PreflightGlue] = Field(max_length=64)
    incompatible: PreflightIncompatible | None

    @model_validator(mode="after")
    def status_agreement(self):
        if self.status == "incompatible":
            if self.incompatible is None or self.glue:
                raise ValueError("incompatible names one reason and no glue")
        elif self.incompatible is not None or self.target is None or self.product_core is None:
            raise ValueError("an admitted result names its target and core, and no reason")
        elif (self.status == "glue") != bool(self.glue):
            raise ValueError("glue is exactly the nonempty conflict list")
        return self

    def provenance_mismatch(self, install: "CatalogInstall") -> str | None:
        """What differs from the saved install payload, or `None` when this is its result."""
        if install.catalog is None:
            return "catalog_unpinned"
        if self.package != install.package.name:
            return "package"
        if self.status == "incompatible":
            return None
        target = self.target
        expected = {
            "catalog_source": install.catalog.repository,
            "catalog_ref": install.catalog.commit,
            "tag": install.package.tag,
            "version": install.package.version,
        }
        for key, value in expected.items():
            if getattr(target, key) != value:
                return f"target.{key}"
        if self.product_core != install.core_version:
            return "product_core"
        return None

    def outstanding_glue(self, install: "CatalogInstall") -> list[PreflightGlue]:
        """Glue the fixed closure does not already perform.

        The kit asks for `kit add <library>` before a package whose binding parses with it;
        the installer adds exactly the closure's libraries, so that item is its own work.
        Everything else is product glue someone has to write.
        """
        closure = {item.name for item in install.libraries}
        return [
            item
            for item in self.glue
            if not (
                item.code == "library_required"
                and item.owner == f"package:{install.package.name}"
                and item.symbol in closure
            )
        ]


#: The kit's owner of a product-side conflict (`framework.host_contract.PRODUCT_OWNER`).
PRODUCT_GLUE_OWNER = "product"
#: `created_by` of the one concrete repair a glue preflight hands to engineering.
GLUE_REPAIR_AUTHOR = "catalog_install_glue"


def product_glue_handoff(preflight: InstallPreflight, install: "CatalogInstall") -> str | None:
    """The concrete repair a worker gets for a glue answer, or `None` when it is no repair.

    Only conflicts the kit assigns to the product are product glue; a conflict owned by a
    package, or a library outside the planned closure, changes the plan and is a person's.
    The text is the kit's own items with their files and actions, and the exact release
    and catalog they were found for — nothing a model chose or rephrased.
    """
    items = preflight.outstanding_glue(install)
    if (
        preflight.status != "glue"
        or not items
        or any(item.owner != PRODUCT_GLUE_OWNER for item in items)
        or preflight.provenance_mismatch(install) is not None
    ):
        return None
    target = preflight.target
    lines = [
        f"Catalog install of {install.package.name} {install.package.version} "
        f"({install.package.tag}, catalog {target.catalog_ref}, metadata "
        f"{target.metadata_sha256}, product core {preflight.product_core}) was refused by the "
        "kit's read-only check-install before any change. Make exactly these product-side "
        "changes, each in the file it names; the platform installs the package itself "
        "afterwards and checks again:",
    ]
    for item in items:
        where = f"{item.path or '-'}:{item.line}" if item.line else (item.path or "-")
        subject = item.symbol or item.key or item.command or "-"
        lines.append(f"- {item.code} at {where} ({subject}): {item.conflict}. Do: {item.action}")
    return "\n".join(lines)


class InstallVerification(Strict):
    core_version: Version
    tooling_commit: SHA
    binding_sha256: Digest
    distributions: dict[Name, Version]
    component_targets: dict[Name, SHA]
    protected_sha256: dict[str, Digest]


#: An attempt checkout is named `<repository id>/<operation id>`, nothing caller-chosen.
AttemptCheckout = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
]


class InstallOperation(Strict):
    """API-owned durable record. No Run, executor, spend, or arbitrary path."""

    id: str
    project_id: uuid.UUID
    task_id: str
    story_id: str
    repository_id: str
    cycle_started_at: datetime
    state: Literal["queued", "running", "published", "refused", "recovery_required"]
    stage: Stage
    token: str | None = None
    heartbeat_at: datetime | None = None
    head_sha: SHA | None = None
    base_sha: SHA | None = None
    detail: str | None = Field(default=None, max_length=2000)
    verification: InstallVerification | None = None
    #: The private checkout this operation's one attempt ran in, by its derived name.
    checkout: AttemptCheckout | None = Field(default=None, exclude_if=lambda value: value is None)
    #: The kit's read-only preflight of this attempt, glue and refusal included.
    preflight: InstallPreflight | None = Field(default=None, exclude_if=lambda value: value is None)


class InstallCommand(Strict):
    operation_id: str | None = None
    token: str | None = None
    action: Literal["admit", "claim", "heartbeat", "checkpoint", "publish", "refuse"]
    stage: Stage | None = None
    head_sha: SHA | None = None
    base_sha: SHA | None = None
    detail: str | None = Field(default=None, max_length=2000)
    verification: InstallVerification | None = None
    checkout: AttemptCheckout | None = None
    preflight: InstallPreflight | None = None


class InstallOperatorRequest(Strict):
    operation_id: str
    action: Literal["retry", "recover", "replan"]
    stop_id: str | None = None


#: The note key every operator settlement of an install operation writes.
SETTLEMENT_NOTE_KEY = "catalog_install_settlement"

#: How long after its transaction opened one operator `replan` may stamp the
#: story's `reopened_at`. The note is written with the database clock of the
#: transaction's start and the reopen later in that same request, after one
#: GitHub read; a later reopen needs a whole new work cycle first.
REPLAN_REOPEN_WINDOW = timedelta(minutes=2)


def is_replan_note(details: dict[str, Any]) -> bool:
    """The note an operator `replan` writes on the install Task it cancelled."""
    return SETTLEMENT_NOTE_KEY in details and details.get("operator_action") == "replan"


def replan_reopened(noted_at: datetime, reopened_at: datetime | None) -> bool:
    """Whether `reopened_at` is the reopen the `replan` noted at `noted_at` stamped.

    True only while the story has not been reopened since that replan, so its
    current work cycle is the one the replan opened.
    """
    if reopened_at is None:
        return False
    noted = noted_at if noted_at.tzinfo else noted_at.replace(tzinfo=UTC)
    reopened = reopened_at if reopened_at.tzinfo else reopened_at.replace(tzinfo=UTC)
    return abs(reopened - noted) <= REPLAN_REOPEN_WINDOW


class InstallDecision(Strict):
    outcome: Literal["admitted", "claimed", "reused", "refused", "settled"]
    reason: str | None = None
    operation: InstallOperation | None = None
    install: CatalogInstall | None = None
    project_name: str | None = None
    git_url: str | None = None
