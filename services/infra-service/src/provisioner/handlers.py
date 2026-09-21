"""Provisioner handlers - success/failure handling and notification logic."""

from collections.abc import Awaitable, Callable

from pydantic import ValidationError
import structlog

from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import (
    ProvisioningFinalization,
    ProvisioningFinalizationDisposition,
    QATargetReceipt,
    TargetIdentity,
)
from shared.diagnostics import safe_validation_errors
from shared.notifications import notify_admins_best_effort
from shared.qa_identity import provisioning_complete_labels
from shared.qa_target_profile import QATargetProof

from .api_client import finalize_provisioning, update_server_status
from .incidents import create_incident
from .recovery import redeploy_all_services
from .ssh_manager import SSHManager

logger = structlog.get_logger()

# The provisioning-failure steps the success handler owns, in execution order.
RECEIPT_STEP = "qa_target_receipt"


class FinalizationOutcomeUnknown(RuntimeError):
    """The API may have committed; the broker delivery must remain retryable."""


async def _fail_provisioning_success(
    server_handle: str,
    server_ip: str,
    *,
    step: str,
    reason: str,
    what_failed: str,
    episode_id: str | None = None,
    identity: TargetIdentity | None = None,
) -> dict:
    """A green play whose success could not be committed is a provisioning failure.

    Owned here exactly as node.py owns its own failure branches: terminal error
    status first, then the provisioning incident that puts the server back into
    the retry cycle. Without both, a later server_sync could re-mark this server
    ACTIVE with nothing in the journal to make it retryable.
    """
    await update_server_status(server_handle, "error")
    details = {"step": step, "reason": reason}
    if episode_id is not None:
        details["episode_id"] = episode_id
    if identity is not None:
        details["identity"] = identity.model_dump(mode="json")
    await create_incident(server_handle, IncidentType.PROVISIONING_FAILED, details)
    await notify_admins_best_effort(
        f"❌ Server *{server_handle}* provisioned, but {what_failed} ({reason}). "
        "The server is NOT ready.",
        level="error",
        server_handle=server_handle,
    )
    return {
        "messages": [
            {"message": f"❌ Provisioning of {server_handle} failed: {what_failed} ({reason})"}
        ],
        "errors": [f"{step} failed: {reason}"],
        "provisioning_result": {
            "status": "failed",
            "reason": reason,
            "server_handle": server_handle,
            "server_ip": server_ip,
        },
        "current_agent": "provisioner",
    }


async def handle_provisioning_success(  # noqa: PLR0911, PLR0913
    server_handle: str,
    server_ip: str,
    provisioning_attempts: int,
    provisioning_episode_id: str,
    is_recovery: bool,
    method_suffix: str = "",
    *,
    ssh_manager: SSHManager | None,
    qa_target_proof: QATargetProof | None,
    expected_identity: TargetIdentity,
    retain_finalization: Callable[[ProvisioningFinalization], Awaitable[None]] | None = None,
) -> dict:
    """Submit key, completion facts, receipt and READY to the one API finalizer.

    A missing proof, a failed completion write, an identity that changed before
    the receipt landed, or a receipt that could not be written is a terminal
    provisioning failure, committed like a key failure: ``error`` and a
    PROVISIONING_FAILED incident, never a READY row without its receipt. An
    attempt a newer episode superseded records neither receipt nor READY.

    Args:
        server_handle: Server handle
        server_ip: Server IP
        provisioning_attempts: Number of attempts
        provisioning_episode_id: The episode this attempt belongs to
        is_recovery: Whether this is incident recovery
        method_suffix: Suffix for message (e.g., " (Reinstalled)")
        ssh_manager: SSHManager holding the private key; None is a failure
        qa_target_proof: The software play's proof of the current QA target
            profile, or None when it proved none
        expected_identity: The server-row identity read before the proof began

    Returns:
        State update dict
    """
    private_key = ssh_manager.get_private_key() if ssh_manager is not None else None
    if not private_key:
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="ssh_private_key_missing",
            what_failed="its SSH key could not be stored",
            episode_id=provisioning_episode_id,
            identity=expected_identity,
        )

    if qa_target_proof is None:
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="qa_target_profile_not_proved",
            what_failed="its software play proved no current QA target profile",
            episode_id=provisioning_episode_id,
            identity=expected_identity,
        )

    try:
        proved_identity = TargetIdentity(
            ssh_user=qa_target_proof.ssh_user,
            host=expected_identity.host,
            public_ip=server_ip,
            ssh_key_fingerprint=qa_target_proof.ssh_key_fingerprint,
        )
        receipt = QATargetReceipt(
            profile_version=qa_target_proof.profile_version,
            proved_at=qa_target_proof.proved_at,
            identity=proved_identity,
        )
        finalization = ProvisioningFinalization(
            attempt_number=provisioning_attempts,
            episode_id=provisioning_episode_id,
            expected_identity=expected_identity,
            proved_identity=proved_identity,
            generated_key_fingerprint=qa_target_proof.ssh_key_fingerprint,
            generated_private_key=private_key,
            complete_labels=provisioning_complete_labels(),
            qa_target_receipt=receipt,
        )
    except ValidationError as exc:
        logger.error(
            "provisioning_finalization_command_invalid",
            server_handle=server_handle,
            validation_errors=safe_validation_errors(exc),
        )
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="finalization_command_invalid",
            what_failed="its finalization command was invalid",
            episode_id=provisioning_episode_id,
            identity=expected_identity,
        )

    try:
        if retain_finalization is None:
            raise RuntimeError("finalization replay persistence is not configured")
        await retain_finalization(finalization)
    except Exception as exc:
        logger.error(
            "provisioning_finalization_replay_unavailable",
            server_handle=server_handle,
            error_type=type(exc).__name__,
        )
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="finalization_replay_unavailable",
            what_failed="its finalization replay could not be retained",
            episode_id=provisioning_episode_id,
            identity=expected_identity,
        )

    try:
        disposition = await finalize_provisioning(server_handle, finalization)
    except Exception as exc:
        logger.error(
            "provisioning_finalization_outcome_unknown",
            server_handle=server_handle,
            error_type=type(exc).__name__,
        )
        raise FinalizationOutcomeUnknown(
            f"Provisioning finalization outcome unknown for {server_handle}"
        ) from exc

    if disposition is ProvisioningFinalizationDisposition.CONFLICT:
        logger.info(
            "provisioning_attempt_reset_skipped",
            server_handle=server_handle,
            attempt=provisioning_attempts,
            ssh_key_persisted=False,
        )
        await create_incident(
            server_handle,
            IncidentType.PROVISIONING_FAILED,
            {
                "step": RECEIPT_STEP,
                "reason": "finalization_conflict",
                "episode_id": provisioning_episode_id,
                "identity": expected_identity.model_dump(mode="json"),
            },
        )
        return {
            "messages": [
                {
                    "message": (
                        f"Provisioning success for {server_handle} was superseded by a concurrent "
                        "server edit or newer attempt"
                    )
                }
            ],
            "provisioning_result": {
                "status": "superseded",
                "server_handle": server_handle,
                "server_ip": server_ip,
            },
            "current_agent": "provisioner",
        }

    if disposition is ProvisioningFinalizationDisposition.CONTAINED:
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="finalization_contained",
            what_failed="its atomic success finalization was refused",
            episode_id=provisioning_episode_id,
            identity=expected_identity,
        )

    recovery_text = "recovered and " if is_recovery else ""
    services_redeployed = 0
    services_failed = 0

    if is_recovery:
        # Redeploy services
        logger.info("service_redeployment_start", server_handle=server_handle)
        services_redeployed, services_failed, errors = await redeploy_all_services(
            server_handle, server_ip
        )

    message = f"""✅ Server {server_handle} {recovery_text}provisioned successfully!{method_suffix}

IP: {server_ip}
Status: READY
Provisioning attempt: {provisioning_attempts}

The server is now configured with:
- SSH key authentication
- Docker and Docker Compose
- UFW firewall
- Essential tools
"""

    if is_recovery and (services_redeployed > 0 or services_failed > 0):
        message += f"\n📦 Services: {services_redeployed} redeployed, {services_failed} failed"
    # Send notification
    await notify_admins_best_effort(
        f"Server *{server_handle}* {recovery_text}provisioned successfully! "
        f"IP: {server_ip}. Server is now READY.",
        level="success",
        server_handle=server_handle,
    )

    return {
        "messages": [{"message": message}],
        "provisioning_result": {
            "status": "success",
            "server_handle": server_handle,
            "server_ip": server_ip,
            "services_redeployed": services_redeployed,
            "services_failed": services_failed,
            "incident_journal_status": "resolved",
        },
        "current_agent": "provisioner",
    }
