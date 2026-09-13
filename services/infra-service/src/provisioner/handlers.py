"""Provisioner handlers - success/failure handling and notification logic."""

import structlog

from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import QATargetReceipt, target_identity
from shared.notifications import notify_admins_best_effort
from shared.qa_target_profile import QATargetProof
from shared.ssh_keys import AdminKeyRejectedError, normalize_admin_private_key

from .api_client import (
    TargetReadinessSupersededError,
    get_server_info,
    mark_provisioning_complete,
    reset_provisioning_attempts,
    save_server_ssh_key,
    update_server_status,
)
from .incidents import create_incident, resolve_active_incidents
from .recovery import redeploy_all_services
from .ssh_manager import SSHManager

logger = structlog.get_logger()

# The provisioning-failure steps the success handler owns, in execution order.
KEY_PERSISTENCE_STEP = "ssh_key_persistence"
COMPLETION_STEP = "provisioning_completion"
RECEIPT_STEP = "qa_target_receipt"


async def _persist_server_ssh_key(
    server_handle: str, ssh_manager: SSHManager | None
) -> tuple[str | None, str | None]:
    """Validate and store the key that grants access to the provisioned server.

    The key lives in the infra-service container's ephemeral filesystem. Until it
    is in the DB, recreating the container loses access to the server forever, so
    a failure here is a provisioning failure, not a skippable side effect.

    Returns:
        ``(None, fingerprint)`` on success, otherwise ``(failure reason, None)``.
    """
    if ssh_manager is None:
        logger.error(
            "provisioning_ssh_key_persist_failed",
            server_handle=server_handle,
            reason="ssh_manager_missing",
        )
        return "ssh_manager_missing", None

    private_key = ssh_manager.get_private_key()
    if not private_key:
        logger.error(
            "provisioning_ssh_key_persist_failed",
            server_handle=server_handle,
            reason="ssh_private_key_missing",
        )
        return "ssh_private_key_missing", None

    try:
        fingerprint = normalize_admin_private_key(private_key).fingerprint
    except AdminKeyRejectedError as exc:
        logger.error(
            "provisioning_ssh_key_persist_failed",
            server_handle=server_handle,
            reason="ssh_private_key_invalid",
            rejection=exc.rejection.value,
        )
        return "ssh_private_key_invalid", None

    try:
        await save_server_ssh_key(server_handle, private_key)
    except Exception as exc:
        logger.error(
            "provisioning_ssh_key_persist_failed",
            server_handle=server_handle,
            reason="save_server_ssh_key_failed",
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return "save_server_ssh_key_failed", None

    return None, fingerprint


async def _fail_provisioning_success(
    server_handle: str, server_ip: str, *, step: str, reason: str, what_failed: str
) -> dict:
    """A green play whose success could not be committed is a provisioning failure.

    Owned here exactly as node.py owns its own failure branches: terminal error
    status first, then the provisioning incident that puts the server back into
    the retry cycle. Without both, a later server_sync could re-mark this server
    ACTIVE with nothing in the journal to make it retryable.
    """
    await update_server_status(server_handle, "error")
    await create_incident(
        server_handle, IncidentType.PROVISIONING_FAILED, {"step": step, "reason": reason}
    )
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
) -> dict:
    """Commit a successful provisioning — key, phase, receipt and READY — in a fixed order.

    1. validate and persist the server's private SSH key — no key, no success;
    2. record the complete software phase and the QA identity it created — only
       now, because a managed row may not reach a complete phase without a key;
    3. bind the software play's QA target proof to the row's connection identity
       with the fingerprint of the key just persisted;
    4. close the provisioning episode via ``reset_provisioning_attempts``, which
       records that receipt and writes the terminal READY status in one row-locked
       transaction — the single owner of that status;
    5. resolve incidents and redeploy services.

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

    Returns:
        State update dict
    """
    key_failure, fingerprint = await _persist_server_ssh_key(server_handle, ssh_manager)
    if key_failure:
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=KEY_PERSISTENCE_STEP,
            reason=key_failure,
            what_failed="its SSH key could not be stored",
        )

    try:
        await mark_provisioning_complete(server_handle)
    except Exception as exc:
        logger.error(
            "provisioning_completion_write_failed",
            server_handle=server_handle,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=COMPLETION_STEP,
            reason=type(exc).__name__,
            what_failed="its completed software phase could not be recorded",
        )

    if qa_target_proof is None:
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="qa_target_profile_not_proved",
            what_failed="its software play proved no current QA target profile",
        )

    try:
        server = await get_server_info(server_handle)
        receipt = QATargetReceipt(
            profile_version=qa_target_proof.profile_version,
            proved_at=qa_target_proof.proved_at,
            identity=target_identity(server, fingerprint),
        )
        reset = await reset_provisioning_attempts(
            server_handle, provisioning_attempts, provisioning_episode_id, receipt
        )
    except TargetReadinessSupersededError:
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="identity_changed",
            what_failed="its connection identity changed before the readiness receipt was recorded",
        )
    except Exception as exc:
        logger.error(
            "provisioning_receipt_write_failed",
            server_handle=server_handle,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return await _fail_provisioning_success(
            server_handle,
            server_ip,
            step=RECEIPT_STEP,
            reason="receipt_write_failed",
            what_failed="its readiness receipt and READY status could not be recorded",
        )
    if not reset:
        logger.info(
            "provisioning_attempt_reset_skipped",
            server_handle=server_handle,
            attempt=provisioning_attempts,
            ssh_key_persisted=True,
        )
        return {
            "messages": [
                {
                    "message": (
                        f"Provisioning success for {server_handle} superseded by a newer attempt "
                        "(its SSH key is stored)"
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

    incident_journal_status = "resolved"
    try:
        await resolve_active_incidents(server_handle)
    except Exception as exc:
        incident_journal_status = "pending_reconciliation"
        logger.error(
            "provisioning_incident_resolution_failed",
            server_handle=server_handle,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        await notify_admins_best_effort(
            f"⚠️ Server *{server_handle}* is READY, but its provisioning incident journal "
            "could not be closed. Reconciliation will retry automatically.",
            level="warning",
            server_handle=server_handle,
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
    if incident_journal_status == "pending_reconciliation":
        message += (
            "\n⚠️ Provisioning incident journal could not be closed; reconciliation will retry."
        )

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
            "incident_journal_status": incident_journal_status,
        },
        "current_agent": "provisioner",
    }
