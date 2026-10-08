"""Finite catalog selection and non-engineering operation ownership."""

from datetime import datetime
from typing import Annotated, Literal
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


class CatalogInstall(Strict):
    package: InstallComponent
    libraries: list[InstallComponent] = Field(max_length=16)
    binding: DefaultBinding
    core_version: Version
    python_version: Version
    catalog_digest: Digest
    tooling_commit: SHA

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


class InstallDecision(Strict):
    outcome: Literal["admitted", "claimed", "reused", "refused", "settled"]
    reason: str | None = None
    operation: InstallOperation | None = None
    install: CatalogInstall | None = None
    project_name: str | None = None
    git_url: str | None = None
