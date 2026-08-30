"""Provisioner node - main orchestration logic.

Handles automated server provisioning:
1. Verifies managed status, provider allowlist, and provider ID/IP binding
2. Reinstalls only after an explicit force-rebuild request
3. Runs Ansible provisioning playbooks
4. Updates server status and handles incident recovery
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, NamedTuple

import httpx
import structlog

from shared.contracts.dto.incident import IncidentType
from shared.contracts.dto.server import ServerDTO, ServerStatus
from shared.notifications import notify_admins_best_effort
from shared.provisioning_policy import (
    TIME4VPS_PROVIDER,
    authorize_run_owned_target,
    authorized_provider_id,
    provider_ip_matches,
)

if TYPE_CHECKING:
    from shared.clients.time4vps import Time4VPSClient

from ..config.constants import Provisioning, Timeouts
from ..nodes import FunctionalNode, log_node_execution
from .ansible_runner import AnsibleRunner
from .api_client import (
    get_server_info,
    get_server_ssh_key,
    mark_provisioning_complete,
    reserve_provisioning_attempt,
    update_server_labels,
    update_server_status,
)
from .bitlaunch import BITLAUNCH_PROVIDER, BitLaunchClient
from .handlers import handle_provisioning_success
from .incidents import create_incident
from .operations import reinstall_and_provision, reset_server_password
from .ssh_manager import SSHManager

logger = structlog.get_logger()

# Configuration from centralized constants
PROVISIONING_MAX_RETRIES = Provisioning.MAX_RETRIES


class AuthorizedServer(NamedTuple):
    """Server identity proven against the database provisioning policy."""

    server: ServerDTO
    provider: str
    provider_id: str
    ip: str


class ProvisioningDenied(Exception):
    """Typed fail-closed rejection returned as a provisioning result."""

    def __init__(
        self, *, reason: str, error: str, message: str, mark_server_error: bool = False
    ) -> None:
        super().__init__(error)
        self.reason = reason
        self.error = error
        self.message = message
        self.mark_server_error = mark_server_error

    def as_result(self, state: dict) -> dict:
        """Convert the rejection to the queue node's result shape."""
        return {
            "messages": [{"message": self.message}],
            "errors": state.get("errors", []) + [self.error],
            "provisioning_result": {"status": "failed", "reason": self.reason},
        }


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

    async def _handle_denial(
        self, server_handle: str, state: dict, denial: ProvisioningDenied
    ) -> dict:
        """Apply the denial's single state-write policy and return its result."""
        if denial.mark_server_error:
            await update_server_status(server_handle, "error")
        return denial.as_result(state)

    async def _get_and_validate_server_info(
        self,
        server_handle: str,
    ) -> AuthorizedServer:
        """Get a server and return its complete authorized provider identity."""
        server_info = await get_server_info(server_handle)

        if server_info.provider == TIME4VPS_PROVIDER:
            authorized_id = authorized_provider_id(
                provider=server_info.provider,
                provider_id=server_info.provider_id,
                is_managed=server_info.is_managed,
            )
        elif server_info.provider == BITLAUNCH_PROVIDER:
            authorized_id = authorize_run_owned_target(
                server_info, run_tag=os.getenv("STAND_RUN_TAG")
            )
        else:
            authorized_id = None
        if authorized_id is None:
            logger.error(
                "provisioning_server_not_authorized",
                server_handle=server_handle,
                provider=server_info.provider,
                server_id=server_info.provider_id,
                is_managed=server_info.is_managed,
            )
            raise ProvisioningDenied(
                reason="server_not_authorized",
                error="Server is not authorized",
                message=f"❌ Server {server_handle} is not authorized.",
            )

        server_ip = server_info.public_ip or server_info.host
        if not server_ip:
            raise ProvisioningDenied(
                reason="server_ip_missing",
                error=f"Missing IP for {server_handle}",
                message=f"❌ Server {server_handle} has no public IP address.",
                mark_server_error=True,
            )

        return AuthorizedServer(
            server=server_info,
            provider=server_info.provider,
            provider_id=authorized_id,
            ip=server_ip,
        )

    async def _init_time4vps_client(
        self,
        server_handle: str,
        target: AuthorizedServer,
    ) -> Time4VPSClient:
        """Initialize Time4VPS and prove that provider ID and IP identify one server."""
        from shared.clients.time4vps import Time4VPSClient

        time4vps_username = os.getenv("TIME4VPS_LOGIN") or os.getenv("TIME4VPS_USERNAME")
        time4vps_password = os.getenv("TIME4VPS_PASSWORD")

        if not time4vps_username or not time4vps_password:
            logger.error("TIME4VPS credentials not configured")
            raise ProvisioningDenied(
                reason="time4vps_credentials_missing",
                error="Missing TIME4VPS credentials",
                message="❌ TIME4VPS credentials not configured",
                mark_server_error=True,
            )

        time4vps_client = Time4VPSClient(time4vps_username, time4vps_password)
        details = await time4vps_client.get_server_details(int(target.provider_id))
        if not provider_ip_matches(expected_ip=target.ip, provider_ip=details.ip):
            logger.error(
                "provisioning_provider_identity_mismatch",
                server_handle=server_handle,
                server_id=target.provider_id,
                database_ip=target.ip,
                provider_ip=details.ip,
            )
            raise ProvisioningDenied(
                reason="provider_identity_mismatch",
                error="Provider identity mismatch",
                message=f"❌ Provider identity mismatch for {server_handle}.",
                mark_server_error=True,
            )

        return time4vps_client

    def _provider_identity_mismatch(self, server_handle: str) -> ProvisioningDenied:
        return ProvisioningDenied(
            reason="provider_identity_mismatch",
            error="Provider identity mismatch",
            message=f"❌ Provider identity mismatch for {server_handle}.",
            mark_server_error=True,
        )

    async def _init_bitlaunch_client(
        self,
        server_handle: str,
        target: AuthorizedServer,
    ) -> BitLaunchClient:
        """Prove the exact run-owned ID/IP pair before touching a BitLaunch target."""
        try:
            client = BitLaunchClient.from_environment()
            provider_ip = await client.get_server_ip(target.provider_id)
        except (ValueError, httpx.HTTPError) as exc:
            logger.error(
                "bitlaunch_observation_failed",
                server_handle=server_handle,
                error_type=type(exc).__name__,
            )
            raise ProvisioningDenied(
                reason="bitlaunch_observation_failed",
                error="BitLaunch observation failed",
                message="❌ BitLaunch could not confirm the target identity.",
                mark_server_error=True,
            ) from exc
        if not provider_ip_matches(expected_ip=target.ip, provider_ip=provider_ip):
            raise self._provider_identity_mismatch(server_handle)
        return client

    async def _run_reinstall_path(  # noqa: PLR0913
        self,
        time4vps_client,
        server_handle: str,
        provider: str,
        server_id: int,
        server_ip: str,
        deploy_user: str,
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
            provider=provider,
            is_managed=True,
            server_id=server_id,
            server_ip=server_ip,
            os_template=os_template,
            ssh_manager=self.ssh_manager,
            ansible_runner=self.ansible_runner,
            ssh_public_key=ssh_public_key,
            deploy_user=deploy_user,
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
        await notify_admins_best_effort(
            f"❌ Server *{server_handle}* reinstall FAILED: {message[:200]}",
            level="error",
            server_handle=server_handle,
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
        deploy_user: str,
        provisioning_attempts: int,
        provisioning_episode_id: str,
        is_recovery: bool,
        state: dict,
        ssh_user: str | None = None,
        ssh_private_key: str | None = None,
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
            deploy_user=deploy_user,
            ssh_user=ssh_user,
            ssh_private_key=ssh_private_key,
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
            ssh_public_key=self.ssh_manager.get_public_key(),
            deploy_user=deploy_user,
            ssh_user=ssh_user,
            ssh_private_key=ssh_private_key,
            orchestrator_ip=self.orchestrator_ip,
            orchestrator_hostname=self.orchestrator_hostname,
            timeout=Timeouts.PROVISIONING,
        )

        if success_soft:
            await mark_provisioning_complete(server_handle)
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
        1. Get and authorize server identity
        2. Reserve an attempt so provider failures consume the retry budget
        3. Verify the provider still binds that ID to the stored IP
        4. Run Ansible, reinstalling only after explicit force-rebuild
        5. Handle incident recovery
        """
        server_handle = state.get("server_to_provision")
        is_recovery = state.get("is_incident_recovery", False)

        if not server_handle:
            return {
                "messages": [{"message": "⚠️ No server specified for provisioning"}],
                "errors": state.get("errors", []) + ["No server_to_provision in state"],
            }

        # Step 1: Get and validate server info
        try:
            target = await self._get_and_validate_server_info(server_handle)
        except ProvisioningDenied as denial:
            return await self._handle_denial(server_handle, state, denial)

        server_info = target.server
        server_id = target.provider_id
        server_ip = target.ip
        os_template = server_info.os_template or Provisioning.DEFAULT_OS_TEMPLATE

        if (
            target.provider == BITLAUNCH_PROVIDER
            and server_info.status == ServerStatus.FORCE_REBUILD
        ):
            return await self._handle_denial(
                server_handle,
                state,
                ProvisioningDenied(
                    reason="bitlaunch_reinstall_unsupported",
                    error="BitLaunch reinstall is not supported",
                    message="❌ BitLaunch targets cannot be force-rebuilt.",
                ),
            )

        bitlaunch_key: str | None = None
        if target.provider == BITLAUNCH_PROVIDER:
            # This read-only provider proof precedes the attempt reservation and
            # status write. A stale ID/IP pair must not alter the target at all.
            try:
                await self._init_bitlaunch_client(server_handle, target)
            except ProvisioningDenied as denial:
                return await self._handle_denial(server_handle, state, denial)
            bitlaunch_key = await get_server_ssh_key(server_handle)
            if not bitlaunch_key:
                return await self._handle_denial(
                    server_handle,
                    state,
                    ProvisioningDenied(
                        reason="bitlaunch_ssh_key_missing",
                        error="BitLaunch target SSH key is missing",
                        message=f"❌ BitLaunch target {server_handle} has no stored SSH key.",
                        mark_server_error=True,
                    ),
                )

        # Step 2: Atomically reserve an attempt after authorization and before mutation.
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

        # Step 3: Time4VPS repeats its provider proof at its destructive boundary.
        time4vps_client = None
        if target.provider == TIME4VPS_PROVIDER:
            try:
                time4vps_client = await self._init_time4vps_client(server_handle, target)
            except ProvisioningDenied as denial:
                return await self._handle_denial(server_handle, state, denial)

        # Step 4: Update status
        await update_server_status(server_handle, "provisioning")

        logger.info(
            "provisioning_start",
            server_handle=server_handle,
            attempt=provisioning_attempts,
        )

        # Step 5: Determine provisioning method and execute
        use_reinstall = server_info.status == ServerStatus.FORCE_REBUILD

        if use_reinstall:
            return await self._run_reinstall_path(
                time4vps_client=time4vps_client,
                server_handle=server_handle,
                provider=target.provider,
                server_id=int(server_id),
                server_ip=server_ip,
                deploy_user=server_info.ssh_user,
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
                deploy_user=server_info.ssh_user,
                provisioning_attempts=provisioning_attempts,
                provisioning_episode_id=provisioning_episode_id,
                is_recovery=is_recovery,
                state=state,
                ssh_user="root" if target.provider == BITLAUNCH_PROVIDER else None,
                ssh_private_key=bitlaunch_key,
            )


provisioner_node = ProvisionerNode()
run = provisioner_node.run
