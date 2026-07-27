"""Provisioner operations - password reset and OS reinstall logic."""

import asyncio
from datetime import UTC, datetime

import structlog

from shared.clients.time4vps import Time4VPSClient
from shared.notifications import notify_admins_best_effort

from ..config.constants import Provisioning, Timeouts
from .ansible_runner import AnsibleRunner
from .api_client import get_server_info, get_server_ssh_key, update_server_labels
from .ssh_manager import SSHManager

logger = structlog.get_logger()

# Configuration from centralized constants
PASSWORD_RESET_TIMEOUT = Timeouts.PASSWORD_RESET
PASSWORD_RESET_POLL_INTERVAL = Provisioning.PASSWORD_RESET_POLL_INTERVAL


async def provision_monitoring_baseline(
    server_handle: str,
    ansible_runner: AnsibleRunner,
    *,
    orchestrator_ip: str | None = None,
    orchestrator_hostname: str | None = None,
) -> tuple[bool, str]:
    """Apply and verify the monitoring role on an already managed server."""
    server = await get_server_info(server_handle)
    if not server.is_managed:
        return False, "Server is not managed"

    server_ip = server.public_ip or server.host
    if not server_ip:
        return False, "Server has no public IP address"

    ssh_private_key = await get_server_ssh_key(server_handle)
    if not ssh_private_key:
        return False, "Server has no stored SSH key"

    success, output = ansible_runner.run_playbook(
        server_ip=server_ip,
        server_handle=server.handle,
        playbook_name="provision_software.yml",
        deploy_user=server.ssh_user,
        ssh_user=server.ssh_user,
        ssh_private_key=ssh_private_key,
        orchestrator_ip=orchestrator_ip,
        orchestrator_hostname=orchestrator_hostname,
        tags=["monitoring"],
        timeout=Timeouts.PROVISIONING,
    )
    if not success:
        logger.error("monitoring_baseline_failed", server_handle=server_handle)
        return False, f"Monitoring baseline failed: {output[:500]}"

    await update_server_labels(
        server_handle,
        {"monitoring_baseline_applied_at": datetime.now(UTC).isoformat()},
    )
    logger.info("monitoring_baseline_complete", server_handle=server_handle)
    return True, "Monitoring baseline applied successfully"


async def reset_server_password(
    time4vps_client: Time4VPSClient,
    server_handle: str,
) -> str | None:
    """Reset server root password and wait for new password.

    Args:
        time4vps_client: Time4VPS API client
        server_handle: Server handle to reset

    Returns:
        New root password if successful, None otherwise
    """
    server_id = await time4vps_client.get_server_id_by_handle(server_handle)
    if not server_id:
        logger.error("password_reset_server_not_found", server_handle=server_handle)
        return None

    try:
        logger.info("password_reset_triggered", server_handle=server_handle, server_id=server_id)
        task_id = await time4vps_client.reset_password(server_id)
        logger.info("password_reset_task_created", task_id=task_id)

        password = await time4vps_client.wait_for_password_reset(
            server_id,
            task_id,
            timeout=PASSWORD_RESET_TIMEOUT,
            poll_interval=PASSWORD_RESET_POLL_INTERVAL,
        )

        logger.info("password_reset_completed", server_handle=server_handle)
        return password

    except TimeoutError as e:
        logger.error("password_reset_timeout", error=str(e))
        return None
    except Exception as e:
        logger.error(
            "password_reset_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return None


async def reinstall_and_provision(  # noqa: PLR0913
    time4vps_client: Time4VPSClient,
    server_handle: str,
    server_id: int,
    server_ip: str,
    os_template: str,
    ssh_manager: SSHManager,
    ansible_runner: AnsibleRunner,
    ssh_public_key: str | None = None,
    deploy_user: str | None = None,
    orchestrator_ip: str | None = None,
    orchestrator_hostname: str | None = None,
) -> tuple[bool, str]:
    """Reinstall OS and provision server.

    Used when password reset is not sufficient (SSH password auth disabled).
    Flow: Reinstall OS -> Reset password -> Ansible with password

    Args:
        time4vps_client: Time4VPS API client
        server_handle: Server handle
        server_id: Time4VPS server ID
        server_ip: Server IP address
        os_template: OS template to install
        ssh_manager: SSH Manager instance
        ansible_runner: Ansible Runner instance
        ssh_public_key: Optional SSH public key
        orchestrator_ip: Optional orchestrator public IP for UFW rules
        orchestrator_hostname: Optional orchestrator hostname for Loki push URL

    Returns:
        Tuple of (success: bool, message: str)
    """
    logger.info("os_reinstall_start", server_handle=server_handle, server_id=server_id)

    try:
        # Step 1: Trigger reinstall
        task_id = await time4vps_client.reinstall_server(
            server_id=server_id, os_template=os_template, ssh_key=ssh_public_key
        )

        logger.info("reinstall_task_created", task_id=task_id)

        await notify_admins_best_effort(
            f"⏳ Server *{server_handle}* OS reinstall started. This will take ~10-15 minutes.",
            level="info",
            server_handle=server_handle,
        )

        # Step 2: Wait for reinstall to complete
        task_result = await time4vps_client.wait_for_task(
            server_id=server_id,
            task_id=task_id,
            timeout=Timeouts.REINSTALL,
            poll_interval=Provisioning.REINSTALL_POLL_INTERVAL,
        )

        logger.info("os_reinstall_completed", server_handle=server_handle)

        # Extract password from reinstall result (task_result is Time4VPSTask model)
        results = task_result.results or ""
        password = time4vps_client.extract_password(results)

        if not password:
            logger.warning("Could not extract password from reinstall. Trying explicit reset...")
            password = await reset_server_password(time4vps_client, server_handle)

        if not password:
            return False, "Could not obtain root password after reinstall"

        # Step 3: Wait for server to boot
        boot_wait = Provisioning.POST_REINSTALL_BOOT_WAIT
        logger.info("Waiting for server to fully boot...", wait_seconds=boot_wait)
        await asyncio.sleep(boot_wait)

        # Step 4: Run Access Phase
        logger.info("Running Phase 1: Access Configuration...")
        success_access, output_access = ansible_runner.run_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_access.yml",
            root_password=password,
            ssh_public_key=ssh_public_key,
            deploy_user=deploy_user,
            orchestrator_ip=orchestrator_ip,
            orchestrator_hostname=orchestrator_hostname,
            timeout=Timeouts.ACCESS_PHASE,
        )

        if not success_access:
            return False, f"Phase 1 (Access) failed: {output_access[:500]}"

        logger.info("Phase 1 complete. SSH Access established.")

        await update_server_labels(server_handle, {"provisioning_phase": "software_installation"})

        await notify_admins_best_effort(
            f"✅ Server *{server_handle}* connectivity established. "
            "Starting software installation...",
            level="info",
            server_handle=server_handle,
        )

        # Step 5: Run Software Phase
        logger.info("Running Phase 2: Software Installation...")
        success_soft, output_soft = ansible_runner.run_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_software.yml",
            root_password=None,  # Use keys now
            ssh_public_key=ssh_public_key,
            deploy_user=deploy_user,
            orchestrator_ip=orchestrator_ip,
            orchestrator_hostname=orchestrator_hostname,
            timeout=Timeouts.PROVISIONING,
        )

        if success_soft:
            await update_server_labels(server_handle, {"provisioning_phase": "complete"})
            return True, "Provisioning (Access + Software) completed successfully"
        else:
            return False, f"Phase 2 (Software) failed: {output_soft[:500]}"

    except TimeoutError as e:
        logger.error("reinstall_timeout", error=str(e))
        return False, f"Reinstall timeout: {e}"
    except Exception as e:
        logger.error(
            "reinstall_operation_error", error=str(e), error_type=type(e).__name__, exc_info=True
        )
        return False, f"Reinstall failed: {e}"
