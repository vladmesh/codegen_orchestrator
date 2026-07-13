"""Provisioner handlers - success/failure handling and notification logic."""

import structlog

from shared.notifications import notify_admins

from .api_client import reset_provisioning_attempts, save_server_ssh_key, update_server_status
from .incidents import resolve_active_incidents
from .recovery import redeploy_all_services
from .ssh_manager import SSHManager

logger = structlog.get_logger()


async def handle_provisioning_success(
    server_handle: str,
    server_ip: str,
    provisioning_attempts: int,
    is_recovery: bool,
    method_suffix: str = "",
    ssh_manager: SSHManager | None = None,
) -> dict:
    """Handle successful provisioning - update status, resolve incidents, redeploy services.

    Args:
        server_handle: Server handle
        server_ip: Server IP
        provisioning_attempts: Number of attempts
        is_recovery: Whether this is incident recovery
        method_suffix: Suffix for message (e.g., " (Reinstalled)")
        ssh_manager: SSHManager to persist the private key to DB

    Returns:
        State update dict
    """
    await reset_provisioning_attempts(server_handle)
    await update_server_status(server_handle, "ready")

    # Persist SSH key to DB for per-server key storage
    if ssh_manager:
        private_key = ssh_manager.get_private_key()
        if private_key:
            await save_server_ssh_key(server_handle, private_key)

    recovery_text = "recovered and " if is_recovery else ""
    services_redeployed = 0
    services_failed = 0

    if is_recovery:
        # Resolve incidents
        await resolve_active_incidents(server_handle)

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
    await notify_admins(
        f"Server *{server_handle}* {recovery_text}provisioned successfully! "
        f"IP: {server_ip}. Server is now READY.",
        level="success",
    )

    return {
        "messages": [{"message": message}],
        "provisioning_result": {
            "status": "success",
            "server_handle": server_handle,
            "server_ip": server_ip,
            "services_redeployed": services_redeployed,
            "services_failed": services_failed,
        },
        "current_agent": "provisioner",
    }
