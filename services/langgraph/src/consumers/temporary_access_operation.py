"""The one proof of a temporary QA access grant or revoke on its recorded target."""

from __future__ import annotations

from datetime import UTC, datetime

from cryptography.fernet import InvalidToken
import structlog

from shared.contracts.dto.deploy_dispatch import DeployDispatchClaim
from shared.contracts.dto.project import ProjectDTO
from shared.contracts.dto.temporary_access import TemporaryAccessGrantDTO, TemporaryAccessStatus
from shared.crypto import decrypt_dict

from ..clients.api import api_client
from ..clients.users_grant import GeneratedServiceGrantClient
from ..deploy_fence import DeployFence, DeployWrite
from ..subgraphs.devops.deployer import claim_dispatch, require_live_lease
from ._qa_redaction import USERS_GRANT_CAPABILITY

logger = structlog.get_logger(__name__)


def _stored_capability(project: ProjectDTO) -> tuple[str | None, str | None]:
    """The project's stored grant capability decrypted in memory, or a bounded failure."""
    encrypted = ((project.config or {}).get("secrets") or {}).get(USERS_GRANT_CAPABILITY)
    if not isinstance(encrypted, str) or not encrypted:
        return None, "capability_unavailable"
    try:
        capability = decrypt_dict({USERS_GRANT_CAPABILITY: encrypted})[USERS_GRANT_CAPABILITY]
    except InvalidToken:
        return None, "capability_unreadable"
    if not capability:
        return None, "capability_unavailable"
    return capability, None


async def admit_temporary_access(task_id: str, fence: DeployFence) -> DeployDispatchClaim:
    """Take or renew this operation Run's admission across the external boundary.

    The dispatch claim is decided against the locked Run row, so a withdrawal that
    landed first refuses it (`DeployDispatchWithdrawnError`), as does a lapsed lease.
    """
    claim = await claim_dispatch(task_id, fence)
    require_live_lease(claim, datetime.now(UTC))
    return claim


async def confirm_current_operation(  # noqa: PLR0913
    *,
    task_id: str,
    project_id: str,
    application_id: int,
    grant: TemporaryAccessGrantDTO,
    operation: str,
    fence: DeployFence,
) -> tuple[DeployDispatchClaim, str | None]:
    """Renew admission, then require the re-read record to still name this operation.

    Returns the renewed claim and, when the record moved on, a bounded reason, so
    a delayed delivery never re-grants after cleanup.
    """
    claim = await admit_temporary_access(task_id, fence)
    current = await api_client.get_temporary_access_grant(grant.id)
    expected_run_id = current.grant_run_id if operation == "grant" else current.revoke_run_id
    expected_status = (
        TemporaryAccessStatus.GRANTING if operation == "grant" else TemporaryAccessStatus.REVOKING
    )
    if current.status is not expected_status or expected_run_id != task_id:
        logger.info(
            "temporary_access_operation_superseded",
            grant_id=grant.id,
            task_id=task_id,
            operation=operation,
            status=current.status.value,
        )
        return claim, "temporary_access_operation_superseded"
    if (
        current.project_id != project_id
        or current.target_application_id != application_id
        or current.target_application_id != grant.target_application_id
        or current.target_base_url != grant.target_base_url
        or current.head_sha != grant.head_sha
        or (current.channel, current.external_id) != (grant.channel, grant.external_id)
    ):
        return claim, "temporary_access_target_mismatch"
    return claim, None


async def apply_temporary_access_operation(  # noqa: PLR0913
    *,
    task_id: str,
    project_id: str,
    project: ProjectDTO,
    application_id: int,
    grant: TemporaryAccessGrantDTO,
    operation: str,
    fence: DeployFence,
) -> str | None:
    """Grant or revoke the record's identity on its exact target, or name why not.

    Admission, the current record and the lease are confirmed before the deploy
    lock is checked with nothing awaited between it and the product call.
    Returned reasons are bounded and never carry the capability.
    """
    capability, failure = _stored_capability(project)
    if failure is not None:
        return failure
    claim, failure = await confirm_current_operation(
        task_id=task_id,
        project_id=project_id,
        application_id=application_id,
        grant=grant,
        operation=operation,
        fence=fence,
    )
    if failure is not None:
        return failure
    client = GeneratedServiceGrantClient(grant.target_base_url)
    require_live_lease(claim, datetime.now(UTC))
    await fence.ensure_held(DeployWrite.PRODUCT_ACCESS)
    if operation == "grant":
        proof = await client.grant_and_resolve(
            channel=grant.channel, external_id=grant.external_id, capability=capability
        )
        if proof.active:
            return None
    else:
        proof = await client.revoke_and_resolve(
            channel=grant.channel, external_id=grant.external_id, capability=capability
        )
        if not proof.active and proof.failure is None:
            return None
    return proof.failure.value if proof.failure is not None else "unverified"
