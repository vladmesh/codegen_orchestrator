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


class InstallVerification(Strict):
    core_version: Version
    tooling_commit: SHA
    binding_sha256: Digest
    distributions: dict[Name, Version]
    component_targets: dict[Name, SHA]
    protected_sha256: dict[str, Digest]


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


class InstallCommand(Strict):
    operation_id: str | None = None
    token: str | None = None
    action: Literal["admit", "claim", "heartbeat", "checkpoint", "publish", "refuse"]
    stage: Stage | None = None
    head_sha: SHA | None = None
    base_sha: SHA | None = None
    detail: str | None = Field(default=None, max_length=2000)
    verification: InstallVerification | None = None


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
