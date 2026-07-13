"""Provisioner node - main orchestration logic.

Handles automated server provisioning:
1. Checks SSH access
2. Resets root password via Time4VPS API (if needed)
3. Runs Ansible provisioning playbooks
4. Updates server status
5. Handles incident recovery with service redeployment
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import ServerDTO
from shared.notifications import notify_admins

if TYPE_CHECKING:
    from shared.clients.time4vps import Time4VPSClient

from ..config.constants import Provisioning, Timeouts
from ..nodes import FunctionalNode, log_node_execution
from .ansible_runner import AnsibleRunner
from .api_client import (
    get_server_info,
    reserve_provisioning_attempt,
    update_server_labels,
    update_server_status,
)
from .handlers import handle_provisioning_success
from .incidents import create_incident
from .operations import reinstall_and_provision, reset_server_password
from .ssh_manager import SSHManager

logger = structlog.get_logger()

# Configuration from centralized constants
PROVISIONING_MAX_RETRIES = Provisioning.MAX_RETRIES

# Re-export extracted names for backward compatibility
__all__ = [
    "ProvisionerNode",
    "handle_provisioning_success",
    "provisioner_node",
    "reinstall_and_provision",
    "reset_server_password",
    "run",
]


class ProvisionerNode(FunctionalNode):
    """Provisioner node for automated server setup and recovery."""

    def __init__(
        self, ssh_manager: SSHManager | None = None, ansible_runner: AnsibleRunner | None = None
    ):
        super().__init__(node_id="provisioner")
        self.ssh_manager = ssh_manager or SSHManager()
        self.ansible_runner = ansible_runner or AnsibleRunner()
        self.orchestrator_ip = os.getenv("ORCHESTRATOR_PUBLIC_IP")
        self.orchestrator_hostname = os.getenv("ORCHESTRATOR_HOSTNAME")

    async def _get_and_validate_server_info(
        self,
        server_handle: str,
        state: dict,
    ) -> tuple[ServerDTO | None, dict | None]:
        """Get server info and validate it has required fields.

        Returns:
            Tuple of (server_info, error_response). If error_response is not None,
            return it from run() immediately.
        """
        server_info = await get_server_info(server_handle)

        server_ip = server_info.public_ip or server_info.host
        if not server_ip:
            await update_server_status(server_handle, "error")
            return None, {
                "messages": [{"message": f"❌ Server {server_handle} has no public IP address."}],
                "errors": state.get("errors", []) + [f"Missing IP for {server_handle}"],
            }

        return server_info, None

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
        from shared.clients.time4vps import Time4VPSClient

        time4vps_username = os.getenv("TIME4VPS_LOGIN") or os.getenv("TIME4VPS_USERNAME")
        time4vps_password = os.getenv("TIME4VPS_PASSWORD")

        if not time4vps_username or not time4vps_password:
            logger.error("TIME4VPS credentials not configured")
            await update_server_status(server_handle, "error")
            return (
                None,
                None,
                {
                    "messages": [{"message": "❌ TIME4VPS credentials not configured"}],
                    "errors": state.get("errors", []) + ["Missing TIME4VPS credentials"],
                },
            )

        time4vps_client = Time4VPSClient(time4vps_username, time4vps_password)

        # Get Time4VPS server ID
        server_id = (server_info.labels or {}).get("time4vps_id")
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
                            {"message": f"❌ Server {server_handle} not found in Time4VPS"}
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
        """Determine if server needs OS reinstall."""
        use_reinstall = False

        if self.ssh_manager.check_ssh_access(server_ip):
            logger.info("ssh_access_ok", server_handle=server_handle)
        else:
            logger.info("ssh_access_failed", server_handle=server_handle)
            use_reinstall = True

        if force_reinstall or server_status == "force_rebuild":
            logger.info("force_reinstall_requested", server_handle=server_handle)
            use_reinstall = True

        return use_reinstall

    async def _run_reinstall_path(
        self,
        time4vps_client,
        server_handle: str,
        server_id: int,
        server_ip: str,
        os_template: str,
        provisioning_attempts: int,
        provisioning_episode_id: str,
        is_recovery: bool,
        state: dict,
    ) -> dict:
        """Execute reinstall provisioning path."""
        ssh_public_key = self.ssh_manager.get_public_key()

        success, message = await reinstall_and_provision(
            time4vps_client=time4vps_client,
            server_handle=server_handle,
            server_id=server_id,
            server_ip=server_ip,
            os_template=os_template,
            ssh_manager=self.ssh_manager,
            ansible_runner=self.ansible_runner,
            ssh_public_key=ssh_public_key,
            orchestrator_ip=self.orchestrator_ip,
            orchestrator_hostname=self.orchestrator_hostname,
        )

        if success:
            return await handle_provisioning_success(
                server_handle,
                server_ip,
                provisioning_attempts,
                provisioning_episode_id,
                is_recovery,
                " (Reinstalled)",
                ssh_manager=self.ssh_manager,
            )

        await update_server_status(server_handle, "error")
        await create_incident(
            server_handle,
            IncidentType.PROVISIONING_FAILED,
            {"step": "reinstall", "message": message},
        )
        await notify_admins(
            f"❌ Server *{server_handle}* reinstall FAILED: {message[:200]}",
            level="error",
        )
        return {
            "messages": [{"message": f"❌ Reinstall failed: {message}"}],
            "errors": state.get("errors", []) + ["Reinstall failed"],
            "provisioning_result": {"status": "failed", "method": "reinstall"},
        }

    async def _run_existing_access_path(
        self,
        server_handle: str,
        server_ip: str,
        provisioning_attempts: int,
        provisioning_episode_id: str,
        is_recovery: bool,
        state: dict,
    ) -> dict:
        """Execute provisioning using existing SSH access."""
        logger.info("provisioning_existing_setup", server_handle=server_handle)

        # Phase 1: Access
        success_access, output_access = self.ansible_runner.run_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_access.yml",
            root_password=None,
            ssh_public_key=self.ssh_manager.get_public_key(),
            orchestrator_ip=self.orchestrator_ip,
            orchestrator_hostname=self.orchestrator_hostname,
            timeout=Timeouts.ACCESS_PHASE,
        )

        if not success_access:
            await update_server_status(server_handle, "error")
            await create_incident(
                server_handle,
                IncidentType.PROVISIONING_FAILED,
                {"step": "access_setup", "output": output_access[:500]},
            )
            return {
                "messages": [{"message": f"❌ Phase 1 (Access) failed for {server_handle}"}],
                "errors": state.get("errors", []) + ["Phase 1 failed"],
            }

        await update_server_labels(server_handle, {"provisioning_phase": "software_installation"})

        # Phase 2: Software
        success_soft, output_soft = self.ansible_runner.run_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_software.yml",
            root_password=None,
            orchestrator_ip=self.orchestrator_ip,
            orchestrator_hostname=self.orchestrator_hostname,
            timeout=Timeouts.PROVISIONING,
        )

        if success_soft:
            await update_server_labels(server_handle, {"provisioning_phase": "complete"})
            return await handle_provisioning_success(
                server_handle,
                server_ip,
                provisioning_attempts,
                provisioning_episode_id,
                is_recovery,
                " (Retried)",
                ssh_manager=self.ssh_manager,
            )

        await update_server_status(server_handle, "error")
        await create_incident(
            server_handle,
            IncidentType.PROVISIONING_FAILED,
            {"step": "software_setup", "output": output_soft[:500]},
        )
        return {
            "messages": [{"message": f"❌ Phase 2 (Software) failed for {server_handle}"}],
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
        """
        server_handle = state.get("server_to_provision")
        is_recovery = state.get("is_incident_recovery", False)
        force_reinstall = state.get("force_reinstall", False)

        if not server_handle:
            return {
                "messages": [{"message": "⚠️ No server specified for provisioning"}],
                "errors": state.get("errors", []) + ["No server_to_provision in state"],
            }

        # Step 1: Get and validate server info
        server_info, error = await self._get_and_validate_server_info(server_handle, state)
        if error:
            return error

        server_ip = server_info.public_ip or server_info.host
        server_status = server_info.status or ""
        os_template = server_info.os_template or Provisioning.DEFAULT_OS_TEMPLATE
        # Step 2: Atomically reserve an attempt before any external provisioning work.
        try:
            reservation = await reserve_provisioning_attempt(
                server_handle, PROVISIONING_MAX_RETRIES
            )
        except Exception as exc:
            logger.error(
                "provisioning_attempt_reservation_failed",
                server_handle=server_handle,
                error=str(exc),
            )
            await update_server_status(server_handle, "error")
            return {
                "messages": [
                    {"message": f"❌ Failed to reserve provisioning attempt for {server_handle}"}
                ],
                "errors": state.get("errors", []) + ["Provisioning attempt reservation failed"],
                "provisioning_result": {"status": "failed", "reason": "attempt_reservation_failed"},
            }

        if reservation is None:
            await update_server_status(server_handle, "error")
            await create_incident(
                server_handle,
                IncidentType.PROVISIONING_FAILED,
                {"reason": f"Max retries ({PROVISIONING_MAX_RETRIES}) exhausted"},
            )
            return {
                "messages": [
                    {
                        "message": (
                            f"❌ Max provisioning attempts ({PROVISIONING_MAX_RETRIES}) "
                            f"exhausted for {server_handle}"
                        )
                    }
                ],
                "errors": state.get("errors", []) + ["Max provisioning attempts exceeded"],
                "provisioning_result": {"status": "failed", "reason": "max_attempts_exhausted"},
            }

        provisioning_attempts, provisioning_episode_id = reservation

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
            attempt=provisioning_attempts,
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
                provisioning_episode_id=provisioning_episode_id,
                is_recovery=is_recovery,
                state=state,
            )
        else:
            return await self._run_existing_access_path(
                server_handle=server_handle,
                server_ip=server_ip,
                provisioning_attempts=provisioning_attempts,
                provisioning_episode_id=provisioning_episode_id,
                is_recovery=is_recovery,
                state=state,
            )


provisioner_node = ProvisionerNode()
run = provisioner_node.run
