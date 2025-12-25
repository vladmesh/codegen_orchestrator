"""Provisioner node - main orchestration logic.

Handles automated server provisioning:
1. Checks SSH access
2. Resets root password via Time4VPS API (if needed)
3. Runs Ansible provisioning playbooks
4. Updates server status
5. Handles incident recovery with service redeployment
"""

import asyncio
import logging
import os

from langchain_core.messages import AIMessage

from shared.notifications import notify_admins

from ..clients.time4vps import Time4VPSClient
from .ansible_runner import PROVISIONING_TIMEOUT, REINSTALL_TIMEOUT, run_ansible_playbook
from .api_client import (
    get_server_info,
    update_server_labels,
    update_server_status,
)
from .incidents import create_incident, resolve_active_incidents
from .recovery import redeploy_all_services
from .ssh import check_ssh_access, get_ssh_public_key

logger = logging.getLogger(__name__)

# Configuration from environment
PROVISIONING_MAX_RETRIES = int(os.getenv("PROVISIONING_MAX_RETRIES", "3"))
PASSWORD_RESET_TIMEOUT = int(os.getenv("PASSWORD_RESET_TIMEOUT", "300"))
PASSWORD_RESET_POLL_INTERVAL = int(os.getenv("PASSWORD_RESET_POLL_INTERVAL", "5"))


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
        logger.error(f"Cannot reset password: server {server_handle} not found")
        return None

    try:
        logger.info(f"Triggering password reset for server {server_handle} (ID: {server_id})")
        task_id = await time4vps_client.reset_password(server_id)
        logger.info(f"Password reset task created: {task_id}")

        password = await time4vps_client.wait_for_password_reset(
            server_id,
            task_id,
            timeout=PASSWORD_RESET_TIMEOUT,
            poll_interval=PASSWORD_RESET_POLL_INTERVAL,
        )

        logger.info(f"Password reset completed for server {server_handle}")
        return password

    except TimeoutError as e:
        logger.error(f"Password reset timeout: {e}")
        return None
    except Exception as e:
        logger.exception(f"Password reset failed: {e}")
        return None


async def reinstall_and_provision(
    time4vps_client: Time4VPSClient,
    server_handle: str,
    server_id: int,
    server_ip: str,
    os_template: str,
    ssh_public_key: str | None = None,
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
        ssh_public_key: Optional SSH public key

    Returns:
        Tuple of (success: bool, message: str)
    """
    logger.info(f"🔄 Starting full OS reinstall for {server_handle} (ID: {server_id})")

    try:
        # Step 1: Trigger reinstall
        task_id = await time4vps_client.reinstall_server(
            server_id=server_id, os_template=os_template, ssh_key=ssh_public_key
        )

        logger.info(f"Reinstall task created: {task_id}. Waiting for completion...")

        await notify_admins(
            f"⏳ Server *{server_handle}* OS reinstall started. This will take ~10-15 minutes.",
            level="info",
        )

        # Step 2: Wait for reinstall to complete
        task_result = await time4vps_client.wait_for_task(
            server_id=server_id,
            task_id=task_id,
            timeout=REINSTALL_TIMEOUT,
            poll_interval=15,
        )

        logger.info(f"OS reinstall completed for {server_handle}")

        # Extract password from reinstall result
        results = task_result.get("results", "")
        password = time4vps_client.extract_password(results)

        if not password:
            logger.warning("Could not extract password from reinstall. Trying explicit reset...")
            password = await reset_server_password(time4vps_client, server_handle)

        if not password:
            return False, "Could not obtain root password after reinstall"

        # Step 3: Wait for server to boot
        logger.info("Waiting 60s for server to fully boot...")
        await asyncio.sleep(60)

        # Step 4: Run Access Phase
        logger.info("Running Phase 1: Access Configuration...")
        success_access, output_access = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_access.yml",
            root_password=password,
            ssh_public_key=ssh_public_key,
            timeout=180,
        )

        if not success_access:
            return False, f"Phase 1 (Access) failed: {output_access[:500]}"

        logger.info("Phase 1 complete. SSH Access established.")

        await update_server_labels(server_handle, {"provisioning_phase": "software_installation"})

        await notify_admins(
            f"✅ Server *{server_handle}* connectivity established. Starting software installation...",
            level="info",
        )

        # Step 5: Run Software Phase
        logger.info("Running Phase 2: Software Installation...")
        success_soft, output_soft = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_software.yml",
            root_password=None,  # Use keys now
            timeout=PROVISIONING_TIMEOUT,
        )

        if success_soft:
            await update_server_labels(server_handle, {"provisioning_phase": "complete"})
            return True, "Provisioning (Access + Software) completed successfully"
        else:
            return False, f"Phase 2 (Software) failed: {output_soft[:500]}"

    except TimeoutError as e:
        logger.error(f"Reinstall timeout: {e}")
        return False, f"Reinstall timeout: {e}"
    except Exception as e:
        logger.exception(f"Reinstall failed: {e}")
        return False, f"Reinstall failed: {e}"


async def handle_provisioning_success(
    server_handle: str,
    server_ip: str,
    provisioning_attempts: int,
    is_recovery: bool,
    method_suffix: str = "",
) -> dict:
    """Handle successful provisioning - update status, resolve incidents, redeploy services.

    Args:
        server_handle: Server handle
        server_ip: Server IP
        provisioning_attempts: Number of attempts
        is_recovery: Whether this is incident recovery
        method_suffix: Suffix for message (e.g., " (Reinstalled)")

    Returns:
        State update dict
    """
    await update_server_status(server_handle, "ready")

    recovery_text = "recovered and " if is_recovery else ""
    services_redeployed = 0
    services_failed = 0

    if is_recovery:
        # Resolve incidents
        await resolve_active_incidents(server_handle)

        # Redeploy services
        logger.info(f"Starting service redeployment on {server_handle}")
        services_redeployed, services_failed, errors = await redeploy_all_services(
            server_handle, server_ip
        )

    message = f"""✅ Server {server_handle} {recovery_text}provisioned successfully!{method_suffix}
    
IP: {server_ip}
Status: READY
Provisioning attempt: {provisioning_attempts + 1}

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
        "messages": [AIMessage(content=message)],
        "provisioning_result": {
            "status": "success",
            "server_handle": server_handle,
            "server_ip": server_ip,
            "services_redeployed": services_redeployed,
            "services_failed": services_failed,
        },
        "current_agent": "provisioner",
    }


async def run(state: dict) -> dict:
    """Run provisioner node.

    Orchestrates server provisioning:
    1. Get server info
    2. Check SSH access
    3. Reset password or reinstall if needed
    4. Run Ansible playbooks
    5. Update server status
    6. Handle incident recovery

    Args:
        state: Graph state

    Returns:
        Updated state with provisioning result
    """
    server_handle = state.get("server_to_provision")
    is_recovery = state.get("is_incident_recovery", False)
    force_reinstall = state.get("force_reinstall", False)

    if not server_handle:
        return {
            "messages": [AIMessage(content="⚠️ No server specified for provisioning")],
            "errors": state.get("errors", []) + ["No server_to_provision in state"],
        }

    # Get server info
    server_info = await get_server_info(server_handle)
    if not server_info:
        return {
            "messages": [AIMessage(content=f"❌ Failed to get server info for {server_handle}")],
            "errors": state.get("errors", []) + ["Server info fetch failed"],
        }

    server_ip = server_info.get("public_ip") or server_info.get("host")
    server_status = server_info.get("status", "")
    os_template = server_info.get("os_template", "kvm-ubuntu-24.04-gpt-x86_64")
    provisioning_attempts = server_info.get("provisioning_attempts", 0)

    if not server_ip:
        await update_server_status(server_handle, "error")
        return {
            "messages": [AIMessage(content=f"❌ Server {server_handle} has no public IP address.")],
            "errors": state.get("errors", []) + [f"Missing IP for {server_handle}"],
        }

    # Check max attempts
    if provisioning_attempts >= PROVISIONING_MAX_RETRIES:
        await update_server_status(server_handle, "error")
        await create_incident(
            server_handle,
            "provisioning_failed",
            {"reason": f"Max retries ({PROVISIONING_MAX_RETRIES}) exceeded"},
        )
        return {
            "messages": [
                AIMessage(
                    content=f"❌ Max provisioning attempts ({PROVISIONING_MAX_RETRIES}) exceeded for {server_handle}"
                )
            ],
            "errors": state.get("errors", []) + ["Max provisioning attempts exceeded"],
        }

    # Update status
    await update_server_status(server_handle, "provisioning")

    # Initialize Time4VPS client
    time4vps_username = os.getenv("TIME4VPS_LOGIN") or os.getenv("TIME4VPS_USERNAME")
    time4vps_password = os.getenv("TIME4VPS_PASSWORD")

    if not time4vps_username or not time4vps_password:
        logger.error("TIME4VPS credentials not configured")
        await update_server_status(server_handle, "error")
        return {
            "messages": [AIMessage(content="❌ TIME4VPS credentials not configured")],
            "errors": state.get("errors", []) + ["Missing TIME4VPS credentials"],
        }

    time4vps_client = Time4VPSClient(time4vps_username, time4vps_password)

    # Get Time4VPS server ID
    server_id = server_info.get("labels", {}).get("time4vps_id")
    if server_id:
        server_id = int(server_id)
    else:
        server_id = await time4vps_client.get_server_id_by_handle(server_handle)
        if not server_id:
            await update_server_status(server_handle, "error")
            return {
                "messages": [AIMessage(content=f"❌ Server {server_handle} not found in Time4VPS")],
                "errors": state.get("errors", []) + ["Server not found in Time4VPS"],
            }

    logger.info(f"Starting provisioning for {server_handle} (attempt {provisioning_attempts + 1})")

    # Decide: reinstall or existing access
    use_reinstall = False
    if check_ssh_access(server_ip):
        logger.info(f"Server {server_handle} is accessible via SSH Key. Skipping reinstall.")
    else:
        logger.info(
            f"Server {server_handle} NOT accessible via SSH Key. Initiating Reinstall flow."
        )
        use_reinstall = True

    # Force reinstall override
    if force_reinstall or server_status == "force_rebuild":
        logger.info(f"Force reinstall requested for {server_handle}")
        use_reinstall = True

    # ===== REINSTALL PATH =====
    if use_reinstall:
        ssh_public_key = get_ssh_public_key()

        success, message = await reinstall_and_provision(
            time4vps_client=time4vps_client,
            server_handle=server_handle,
            server_id=server_id,
            server_ip=server_ip,
            os_template=os_template,
            ssh_public_key=ssh_public_key,
        )

        if success:
            return await handle_provisioning_success(
                server_handle, server_ip, provisioning_attempts, is_recovery, " (Reinstalled)"
            )
        else:
            await update_server_status(server_handle, "error")
            await create_incident(server_handle, "reinstall_failed", {"message": message})
            await notify_admins(
                f"❌ Server *{server_handle}* reinstall FAILED: {message[:200]}", level="error"
            )
            return {
                "messages": [AIMessage(content=f"❌ Reinstall failed: {message}")],
                "errors": state.get("errors", []) + ["Reinstall failed"],
                "provisioning_result": {"status": "failed", "method": "reinstall"},
            }

    # ===== EXISTING ACCESS PATH =====
    else:
        logger.info(f"Running provisioning playbooks on existing setup for {server_handle}")

        # Phase 1: Access
        success_access, output_access = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_access.yml",
            root_password=None,
            ssh_public_key=get_ssh_public_key(),
            timeout=180,
        )

        if not success_access:
            await update_server_status(server_handle, "error")
            await create_incident(
                server_handle,
                "provisioning_failed",
                {"step": "access_setup", "output": output_access[:500]},
            )
            return {
                "messages": [AIMessage(content=f"❌ Phase 1 (Access) failed for {server_handle}")],
                "errors": state.get("errors", []) + ["Phase 1 failed"],
            }

        await update_server_labels(server_handle, {"provisioning_phase": "software_installation"})

        # Phase 2: Software
        success_soft, output_soft = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_software.yml",
            root_password=None,
            timeout=PROVISIONING_TIMEOUT,
        )

        if success_soft:
            await update_server_labels(server_handle, {"provisioning_phase": "complete"})
            return await handle_provisioning_success(
                server_handle, server_ip, provisioning_attempts, is_recovery, " (Retried)"
            )
        else:
            await update_server_status(server_handle, "error")
            await create_incident(
                server_handle,
                "provisioning_failed",
                {"step": "software_setup", "output": output_soft[:500]},
            )
            return {
                "messages": [
                    AIMessage(content=f"❌ Phase 2 (Software) failed for {server_handle}")
                ],
                "errors": state.get("errors", []) + ["Phase 2 failed"],
            }
