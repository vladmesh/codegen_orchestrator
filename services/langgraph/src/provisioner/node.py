"""Provisioner node - main orchestration logic.

Handles automated server provisioning:
1. Checks SSH access
2. Resets root password via Time4VPS API (if needed)
3. Runs Ansible provisioning playbooks
4. Updates server status
5. Handles incident recovery with service redeployment
"""

import asyncio
import os

from langchain_core.messages import AIMessage
import structlog

from shared.clients.time4vps import Time4VPSClient
from shared.notifications import notify_admins

from ..config.constants import Provisioning, Timeouts
from ..nodes.base import FunctionalNode, log_node_execution
from .ansible_runner import run_ansible_playbook
from .api_client import (
    get_server_info,
    update_server_labels,
    update_server_status,
)
from .incidents import create_incident, resolve_active_incidents
from .recovery import redeploy_all_services
from .ssh import check_ssh_access, get_ssh_public_key

logger = structlog.get_logger()

# Configuration from centralized constants
PROVISIONING_MAX_RETRIES = Provisioning.MAX_RETRIES
PASSWORD_RESET_TIMEOUT = Timeouts.PASSWORD_RESET
PASSWORD_RESET_POLL_INTERVAL = Provisioning.PASSWORD_RESET_POLL_INTERVAL


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
    logger.info("os_reinstall_start", server_handle=server_handle, server_id=server_id)

    try:
        # Step 1: Trigger reinstall
        task_id = await time4vps_client.reinstall_server(
            server_id=server_id, os_template=os_template, ssh_key=ssh_public_key
        )

        logger.info("reinstall_task_created", task_id=task_id)

        await notify_admins(
            f"⏳ Server *{server_handle}* OS reinstall started. This will take ~10-15 minutes.",
            level="info",
        )

        # Step 2: Wait for reinstall to complete
        task_result = await time4vps_client.wait_for_task(
            server_id=server_id,
            task_id=task_id,
            timeout=Timeouts.REINSTALL,
            poll_interval=Provisioning.REINSTALL_POLL_INTERVAL,
        )

        logger.info("os_reinstall_completed", server_handle=server_handle)

        # Extract password from reinstall result
        results = task_result.get("results", "")
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
        success_access, output_access = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_access.yml",
            root_password=password,
            ssh_public_key=ssh_public_key,
            timeout=Timeouts.ACCESS_PHASE,
        )

        if not success_access:
            return False, f"Phase 1 (Access) failed: {output_access[:500]}"

        logger.info("Phase 1 complete. SSH Access established.")

        await update_server_labels(server_handle, {"provisioning_phase": "software_installation"})

        await notify_admins(
            f"✅ Server *{server_handle}* connectivity established. "
            "Starting software installation...",
            level="info",
        )

        # Step 5: Run Software Phase
        logger.info("Running Phase 2: Software Installation...")
        success_soft, output_soft = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_software.yml",
            root_password=None,  # Use keys now
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
        logger.error("reinstall_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
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
        logger.info("service_redeployment_start", server_handle=server_handle)
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


class ProvisionerNode(FunctionalNode):
    """Provisioner node for automated server setup and recovery."""

    def __init__(self):
        super().__init__(node_id="provisioner")

    async def _get_and_validate_server_info(
        self,
        server_handle: str,
        state: dict,
    ) -> tuple[dict | None, dict | None]:
        """Get server info and validate it has required fields.

        Returns:
            Tuple of (server_info, error_response). If error_response is not None,
            return it from run() immediately.
        """
        server_info = await get_server_info(server_handle)
        if not server_info:
            return None, {
                "messages": [
                    AIMessage(content=f"❌ Failed to get server info for {server_handle}")
                ],
                "errors": state.get("errors", []) + ["Server info fetch failed"],
            }

        server_ip = server_info.get("public_ip") or server_info.get("host")
        if not server_ip:
            await update_server_status(server_handle, "error")
            return None, {
                "messages": [
                    AIMessage(content=f"❌ Server {server_handle} has no public IP address.")
                ],
                "errors": state.get("errors", []) + [f"Missing IP for {server_handle}"],
            }

        return server_info, None

    async def _check_max_attempts(
        self,
        server_handle: str,
        provisioning_attempts: int,
        state: dict,
    ) -> dict | None:
        """Check if max provisioning attempts exceeded.

        Returns:
            Error response if max attempts exceeded, None otherwise.
        """
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
                        content=(
                            f"❌ Max provisioning attempts ({PROVISIONING_MAX_RETRIES}) "
                            f"exceeded for {server_handle}"
                        )
                    )
                ],
                "errors": state.get("errors", []) + ["Max provisioning attempts exceeded"],
            }
        return None

    async def _init_time4vps_client(
        self,
        server_handle: str,
        server_info: dict,
        state: dict,
    ) -> tuple[Time4VPSClient | None, int | None, dict | None]:
        """Initialize Time4VPS client and get server ID.

        Returns:
            Tuple of (client, server_id, error_response).
        """
        time4vps_username = os.getenv("TIME4VPS_LOGIN") or os.getenv("TIME4VPS_USERNAME")
        time4vps_password = os.getenv("TIME4VPS_PASSWORD")

        if not time4vps_username or not time4vps_password:
            logger.error("TIME4VPS credentials not configured")
            await update_server_status(server_handle, "error")
            return (
                None,
                None,
                {
                    "messages": [AIMessage(content="❌ TIME4VPS credentials not configured")],
                    "errors": state.get("errors", []) + ["Missing TIME4VPS credentials"],
                },
            )

        time4vps_client = Time4VPSClient(time4vps_username, time4vps_password)

        # Get Time4VPS server ID
        server_id = server_info.get("labels", {}).get("time4vps_id")
        if server_id:
            server_id = int(server_id)
        else:
            server_id = await time4vps_client.get_server_id_by_handle(server_handle)
            if not server_id:
                await update_server_status(server_handle, "error")
                return (
                    None,
                    None,
                    {
                        "messages": [
                            AIMessage(content=f"❌ Server {server_handle} not found in Time4VPS")
                        ],
                        "errors": state.get("errors", []) + ["Server not found in Time4VPS"],
                    },
                )

        return time4vps_client, server_id, None

    def _should_reinstall(
        self,
        server_ip: str,
        server_handle: str,
        server_status: str,
        force_reinstall: bool,
    ) -> bool:
        """Determine if server needs OS reinstall.

        Returns:
            True if reinstall is needed, False if existing access can be used.
        """
        use_reinstall = False

        if check_ssh_access(server_ip):
            logger.info("ssh_access_ok", server_handle=server_handle)
        else:
            logger.info("ssh_access_failed", server_handle=server_handle)
            use_reinstall = True

        # Force reinstall override
        if force_reinstall or server_status == "force_rebuild":
            logger.info("force_reinstall_requested", server_handle=server_handle)
            use_reinstall = True

        return use_reinstall

    async def _run_reinstall_path(
        self,
        time4vps_client: Time4VPSClient,
        server_handle: str,
        server_id: int,
        server_ip: str,
        os_template: str,
        provisioning_attempts: int,
        is_recovery: bool,
        state: dict,
    ) -> dict:
        """Execute reinstall provisioning path.

        Returns:
            State update dict.
        """
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

        await update_server_status(server_handle, "error")
        await create_incident(server_handle, "reinstall_failed", {"message": message})
        await notify_admins(
            f"❌ Server *{server_handle}* reinstall FAILED: {message[:200]}",
            level="error",
        )
        return {
            "messages": [AIMessage(content=f"❌ Reinstall failed: {message}")],
            "errors": state.get("errors", []) + ["Reinstall failed"],
            "provisioning_result": {"status": "failed", "method": "reinstall"},
        }

    async def _run_existing_access_path(
        self,
        server_handle: str,
        server_ip: str,
        provisioning_attempts: int,
        is_recovery: bool,
        state: dict,
    ) -> dict:
        """Execute provisioning using existing SSH access.

        Returns:
            State update dict.
        """
        logger.info("provisioning_existing_setup", server_handle=server_handle)

        # Phase 1: Access
        success_access, output_access = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_access.yml",
            root_password=None,
            ssh_public_key=get_ssh_public_key(),
            timeout=Timeouts.ACCESS_PHASE,
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
            timeout=Timeouts.PROVISIONING,
        )

        if success_soft:
            await update_server_labels(server_handle, {"provisioning_phase": "complete"})
            return await handle_provisioning_success(
                server_handle, server_ip, provisioning_attempts, is_recovery, " (Retried)"
            )

        await update_server_status(server_handle, "error")
        await create_incident(
            server_handle,
            "provisioning_failed",
            {"step": "software_setup", "output": output_soft[:500]},
        )
        return {
            "messages": [AIMessage(content=f"❌ Phase 2 (Software) failed for {server_handle}")],
            "errors": state.get("errors", []) + ["Phase 2 failed"],
        }

    @log_node_execution("provisioner")
    async def run(self, state: dict) -> dict:
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

        # Step 1: Get and validate server info
        server_info, error = await self._get_and_validate_server_info(server_handle, state)
        if error:
            return error

        server_ip = server_info.get("public_ip") or server_info.get("host")
        server_status = server_info.get("status", "")
        os_template = server_info.get("os_template", Provisioning.DEFAULT_OS_TEMPLATE)
        provisioning_attempts = server_info.get("provisioning_attempts", 0)

        # Step 2: Check max attempts
        error = await self._check_max_attempts(server_handle, provisioning_attempts, state)
        if error:
            return error

        # Step 3: Update status
        await update_server_status(server_handle, "provisioning")

        # Step 4: Initialize Time4VPS client
        time4vps_client, server_id, error = await self._init_time4vps_client(
            server_handle, server_info, state
        )
        if error:
            return error

        logger.info(
            "provisioning_start",
            server_handle=server_handle,
            attempt=provisioning_attempts + 1,
        )

        # Step 5: Determine provisioning method and execute
        use_reinstall = self._should_reinstall(
            server_ip, server_handle, server_status, force_reinstall
        )

        if use_reinstall:
            return await self._run_reinstall_path(
                time4vps_client=time4vps_client,
                server_handle=server_handle,
                server_id=server_id,
                server_ip=server_ip,
                os_template=os_template,
                provisioning_attempts=provisioning_attempts,
                is_recovery=is_recovery,
                state=state,
            )
        else:
            return await self._run_existing_access_path(
                server_handle=server_handle,
                server_ip=server_ip,
                provisioning_attempts=provisioning_attempts,
                is_recovery=is_recovery,
                state=state,
            )


provisioner_node = ProvisionerNode()
run = provisioner_node.run
