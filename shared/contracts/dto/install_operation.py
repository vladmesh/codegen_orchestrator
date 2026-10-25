"""The one API-owned INSTALL operation, its owner's commands and the API's decisions.

Either route travels through it: a catalog closure with `InstallVerification`, or a
`LocalPackageInstall` with `LocalInstallVerification`; neither proof validates as the other.
"""

from datetime import datetime
from typing import Annotated, Literal
import uuid

from pydantic import Field

from shared.contracts.dto.catalog_install import (
    SHA,
    InstallPreflight,
    InstallVerification,
    Strict,
)
from shared.contracts.dto.local_install import (
    InstallTarget,
    LocalInstallVerification,
    LocalPackageInstall,
    local_verification_mismatch,
)

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

#: An attempt checkout is named `<repository id>/<operation id>`, nothing caller-chosen.
AttemptCheckout = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
]

#: The proof of one installation, in its target's own route.
OperationVerification = InstallVerification | LocalInstallVerification


def verification_refusal(
    current: OperationVerification | None, proof: OperationVerification, install: InstallTarget
) -> str | None:
    """Why `proof` may not become an operation's verification, or `None` when it may.

    A proof is its target's route. A catalog proof is fixed once accepted; a local one
    advances once, from its provenance to the readback of that provenance, then only repeats.
    """
    if isinstance(install, LocalPackageInstall):
        if not isinstance(proof, LocalInstallVerification):
            return "verification_route_mismatch"
        if proof == current:
            return None
        if current is not None and (current.mode, proof.mode) != ("provenance", "readback"):
            return "verification_changed"
        mismatch = local_verification_mismatch(proof, install, current)
        if mismatch == "provenance":
            return "verification_provenance_missing"
        return None if mismatch is None else "verification_mismatch"
    if not isinstance(proof, InstallVerification):
        return "verification_route_mismatch"
    expected = {item.name: item.version for item in [install.package, *install.libraries]}
    if (
        proof.core_version != install.core_version
        or proof.tooling_commit != install.tooling_commit
        or proof.binding_sha256 != install.binding.sha256
        or proof.distributions != expected
        or set(proof.component_targets) != set(expected)
    ):
        return "verification_mismatch"
    if current is not None and current != proof:
        return "verification_changed"
    return None


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
    #: A local proof advances once, from its provenance to the matching readback.
    verification: OperationVerification | None = None
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
    verification: OperationVerification | None = None
    checkout: AttemptCheckout | None = None
    preflight: InstallPreflight | None = None


class InstallDecision(Strict):
    outcome: Literal["admitted", "claimed", "reused", "refused", "settled"]
    reason: str | None = None
    operation: InstallOperation | None = None
    install: InstallTarget | None = None
    project_name: str | None = None
    git_url: str | None = None
