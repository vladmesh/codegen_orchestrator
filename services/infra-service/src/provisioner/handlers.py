"""Provisioner handlers - success/failure handling and notification logic."""

import structlog

from shared.notifications import notify_admins_best_effort

from .api_client import reset_provisioning_attempts, save_server_ssh_key
from .incidents import resolve_active_incidents
from .recovery import redeploy_all_services
from .ssh_manager import SSHManager

logger = structlog.get_logger()


async def _persist_server_ssh_key(server_handle: str, ssh_manager: SSHManager | None) -> str | None:
    """Store the key that grants access to the provisioned server.

    The key lives in the infra-service container's ephemeral filesystem. Until it
    is in the DB, recreating the container loses access to the server forever, so
    a failure here is a provisioning failure, not a skippable side effect.

    Returns:
        None on success, otherwise the failure reason.
    """
    if ssh_manager is None:
        logger.error(
            "provisioning_ssh_key_persist_failed",
            server_handle=server_handle,
            reason="ssh_manager_missing",
        )
        return "ssh_manager_missing"

    private_key = ssh_manager.get_private_key()
    if not private_key:
        logger.error(
            "provisioning_ssh_key_persist_failed",
            server_handle=server_handle,
            reason="ssh_private_key_missing",
        )
        return "ssh_private_key_missing"

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
        return "save_server_ssh_key_failed"

    return None


async def handle_provisioning_success(
    server_handle: str,
    server_ip: str,
    provisioning_attempts: int,
    provisioning_episode_id: str,
    is_recovery: bool,
    method_suffix: str = "",
    *,
    ssh_manager: SSHManager | None,
) -> dict:
    """Handle successful provisioning - persist key, close episode, resolve incidents.

    Success is committed in one fixed order on every path:
    1. persist the server's private SSH key to the DB — no key, no success;
    2. close the provisioning episode via ``reset_provisioning_attempts``, which
       atomically clears the counter and writes the terminal READY status; it is
       the single owner of that status;
    3. resolve incidents and redeploy services.

    Args:
        server_handle: Server handle
        server_ip: Server IP
        provisioning_attempts: Number of attempts
        is_recovery: Whether this is incident recovery
        method_suffix: Suffix for message (e.g., " (Reinstalled)")
        ssh_manager: SSHManager holding the private key; None is a failure

    Returns:
        State update dict
    """
    key_failure = await _persist_server_ssh_key(server_handle, ssh_manager)
    if key_failure:
        await notify_admins_best_effort(
            f"❌ Server *{server_handle}* provisioned, but its SSH key could not be stored "
            f"({key_failure}). The server is NOT ready.",
            level="error",
            server_handle=server_handle,
        )
        return {
            "messages": [
                {
                    "message": (
                        f"❌ Provisioning of {server_handle} failed: the private SSH key "
                        f"could not be persisted ({key_failure})"
                    )
                }
            ],
            "errors": [f"SSH key persistence failed: {key_failure}"],
            "provisioning_result": {
                "status": "failed",
                "reason": key_failure,
                "server_handle": server_handle,
                "server_ip": server_ip,
            },
            "current_agent": "provisioner",
        }

    reset = await reset_provisioning_attempts(
        server_handle, provisioning_attempts, provisioning_episode_id
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
