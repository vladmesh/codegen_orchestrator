"""Provisioner agent node.

Handles automated server provisioning:
1. Resets root password via Time4VPS API
2. Runs Ansible provisioning playbook
3. Updates server status
4. Handles incident recovery with service redeployment
"""

import logging
import os
import subprocess
import tempfile

import httpx
from langchain_core.messages import AIMessage

from shared.notifications import notify_admins

from ..clients.time4vps import Time4VPSClient

logger = logging.getLogger(__name__)

# Configuration from environment
PROVISIONING_TIMEOUT = int(os.getenv("PROVISIONING_TIMEOUT", "1200"))
PROVISIONING_MAX_RETRIES = int(os.getenv("PROVISIONING_MAX_RETRIES", "3"))
PASSWORD_RESET_TIMEOUT = int(os.getenv("PASSWORD_RESET_TIMEOUT", "300"))
PASSWORD_RESET_POLL_INTERVAL = int(os.getenv("PASSWORD_RESET_POLL_INTERVAL", "5"))


def get_ssh_public_key() -> str | None:
    """Read SSH public key from default location."""
    key_path = os.path.expanduser("~/.ssh/id_ed25519.pub")
    if os.path.exists(key_path):
        with open(key_path) as f:
            return f.read().strip()
    return None


def check_ssh_access(server_ip: str, timeout: int = 10) -> bool:
    """Check if server is accessible via SSH key.

    Args:
        server_ip: Server IP
        timeout: Check timeout

    Returns:
        True if accessible
    """
    cmd = [
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        f"root@{server_ip}",
        "echo success",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0 and "success" in result.stdout
    except Exception:
        return False


# Original get_ssh_public_key (now replaced by the simpler one above)
# def get_ssh_public_key() -> str | None:
#     """Read SSH public key from mounted ~/.ssh directory.
#
#     Tries common key types in order of preference.
#
#     Returns:
#         Public key string or None if not found
#     """
#     key_paths = [
#         "/root/.ssh/id_ed25519.pub",
#         "/root/.ssh/id_rsa.pub",
#         "/root/.ssh/id_ecdsa.pub",
#     ]
#
#     for path in key_paths:
#         if os.path.exists(path):
#             try:
#                 with open(path) as f:
#                     key = f.read().strip()
#                     logger.info(f"Loaded SSH public key from {path}")
#                     return key
#             except Exception as e:
#                 logger.warning(f"Failed to read {path}: {e}")
#
#     logger.error("No SSH public key found in /root/.ssh/")
#     return None


async def get_services_on_server_for_redeployment(server_handle: str) -> list[dict]:
    """Get services deployed on a server for redeployment.

    Args:
        server_handle: Server handle

    Returns:
        List of service deployment records
    """
    api_url = os.getenv("API_URL", "http://api:8000")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_url}/api/servers/{server_handle}/services")
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Failed to get services for {server_handle}: {e}")
        return []


async def get_server_id_from_time4vps(
    time4vps_client: Time4VPSClient, server_handle: str
) -> int | None:
    """Get server_id from Time4VPS API by matching handle.

    Args:
        time4vps_client: Time4VPS API client
        server_handle: Server handle (e.g., 'vps-267179')

    Returns:
        server_id if found, None otherwise
    """
    try:
        servers = await time4vps_client.get_servers()
        logger.info(f"Time4VPS returned {len(servers)} servers")

        for server in servers:
            if not isinstance(server, dict):
                logger.warning(f"Unexpected server entry format: {server}")
                continue

            srv_id = server.get("id") or server.get("server_id")
            if not srv_id:
                logger.warning(f"Server entry missing ID: {server}")
                continue

            # Match by handle (vps-{id})
            if f"vps-{srv_id}" == server_handle:
                return srv_id

        logger.error(
            f"Server {server_handle} not found in Time4VPS API (scanned {len(servers)} servers)"
        )
        return None
    except Exception as e:
        logger.exception(f"Failed to get server ID: {e}")
        return None


async def reset_server_password(time4vps_client: Time4VPSClient, server_handle: str) -> str | None:
    """Reset server root password and wait for new password.

    Args:
        time4vps_client: Time4VPS API client
        server_handle: Server handle to reset

    Returns:
        New root password if successful, None otherwise
    """
    # Get server_id from Time4VPS
    server_id = await get_server_id_from_time4vps(time4vps_client, server_handle)
    if not server_id:
        logger.error(f"Cannot reset password: server {server_handle} not found")
        return None

    try:
        # Trigger password reset
        logger.info(f"Triggering password reset for server {server_handle} (ID: {server_id})")
        task_id = await time4vps_client.reset_password(server_id)
        logger.info(f"Password reset task created: {task_id}")

        # Wait for password
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


def run_ansible_playbook(
    server_ip: str,
    server_handle: str,
    playbook_name: str,
    root_password: str | None = None,
    ssh_public_key: str | None = None,
    timeout: int = 600,
) -> tuple[bool, str]:
    """Run an Ansible playbook.

    Args:
        server_ip: Server IP address
        server_handle: Server handle
        playbook_name: Name of playbook file (e.g., 'provision_access.yml')
        root_password: Optional root password (if None, uses SSH key auth)
        ssh_public_key: Optional SSH public key to inject (extra_vars)
        timeout: execution timeout

    Returns:
        Tuple of (success: bool, output: str)
    """
    playbook_path = f"/app/services/infrastructure/ansible/playbooks/{playbook_name}"

    # Inventory construction
    if root_password:
        # Password authentication
        inventory_content = f"""[target]
{server_ip} ansible_user=root ansible_ssh_pass={root_password} ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
"""
    else:
        # Key authentication (uses default SSH key from ~/.ssh)
        inventory_content = f"""[target]
{server_ip} ansible_user=root ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
"""

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".ini") as inv_file:
        inv_file.write(inventory_content)
        inventory_path = inv_file.name

    # Extra vars for playbook
    extra_vars = f"target_host={server_ip} server_hostname={server_handle}"

    if ssh_public_key:
        extra_vars += f" ssh_public_key='{ssh_public_key}'"

    # Construct ansible-playbook command
    cmd = [
        "ansible-playbook",
        "-i",
        inventory_path,
        playbook_path,
        "--extra-vars",
        extra_vars,
        "-v",
    ]

    auth_mode = "password" if root_password else "key"
    logger.info(f"Running '{playbook_name}' for {server_handle} at {server_ip} (auth: {auth_mode})")

    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

        # Log output (abbreviated)
        stdout_brief = (
            process.stdout[:1000] + "..." if len(process.stdout) > 1000 else process.stdout
        )
        logger.info(f"Ansible stdout:\n{stdout_brief}")

        if process.stderr:
            logger.warning(f"Ansible stderr:\n{process.stderr}")

        success = process.returncode == 0
        if success:
            output = process.stdout
        else:
            # On failure, capture stderr and the LAST 1000 chars of stdout which likely contains the task failure
            stdout_tail = process.stdout[-1000:] if len(process.stdout) > 1000 else process.stdout
            output = f"STDERR: {process.stderr}\n\nSTDOUT TAIL:\n{stdout_tail}"

        return success, output

    except subprocess.TimeoutExpired:
        logger.error(f"Playbook {playbook_name} timeout after {timeout}s")
        return False, f"Timeout after {timeout}s"
    except Exception as e:
        logger.exception(f"Provisioning exception: {e}")
        return False, str(e)
    finally:
        # Cleanup
        if os.path.exists(inventory_path):
            os.remove(inventory_path)


async def update_server_status_in_db(server_handle: str, status: str) -> bool:
    """Update server status in database via API.

    Args:
        server_handle: Server handle
        status: New status

    Returns:
        True if successful
    """
    import httpx

    api_url = os.getenv("API_URL", "http://api:8000")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(
                f"{api_url}/api/servers/{server_handle}", json={"status": status}
            )
            resp.raise_for_status()
            logger.info(f"Updated server {server_handle} status to {status}")
            return True
    except Exception as e:
        logger.error(f"Failed to update server status: {e}")
        return False


async def update_server_labels_in_db(server_handle: str, labels: dict) -> bool:
    """Update server labels in database via API.

    Args:
        server_handle: Server handle
        labels: New labels dict (will be merged/replaced depending on API impl, usually replace)

    Returns:
        True if successful
    """
    import httpx

    api_url = os.getenv("API_URL", "http://api:8000")

    try:
        async with httpx.AsyncClient() as client:
            # First get existing to merge? Or just patch?
            # Assuming patch updates fields provided.
            # If labels logic on API replaces, we should be careful.
            # But for provisioning status, overwriting is probably fine or we can fetch-merge.
            # Let's assume we just want to set this specific label.

            # Fetch current to merge safely (best practice)
            resp = await client.get(f"{api_url}/api/servers/{server_handle}")
            if resp.status_code == 200:
                current_labels = resp.json().get("labels", {}) or {}
                # Update with new labels
                current_labels.update(labels)
                final_labels = current_labels
            else:
                final_labels = labels

            resp = await client.patch(
                f"{api_url}/api/servers/{server_handle}", json={"labels": final_labels}
            )
            resp.raise_for_status()
            logger.info(f"Updated server {server_handle} labels to {final_labels}")
            return True
    except Exception as e:
        logger.error(f"Failed to update server labels: {e}")
        return False


async def create_incident_in_db(server_handle: str, incident_type: str, details: dict) -> bool:
    """Create incident record in database.

    Args:
        server_handle: Server handle
        incident_type: Type of incident
        details: Incident details

    Returns:
        True if successful
    """
    import httpx

    api_url = os.getenv("API_URL", "http://api:8000")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{api_url}/api/incidents/",
                json={
                    "server_handle": server_handle,
                    "incident_type": incident_type,
                    "details": details,
                    "affected_services": [],
                },
            )
            resp.raise_for_status()
            logger.info(f"Created incident for server {server_handle}: {incident_type}")
            return True
    except Exception as e:
        logger.error(f"Failed to create incident: {e}")
        return False


async def resolve_active_incidents(server_handle: str) -> bool:
    """Resolve all active incidents for a server after successful recovery.

    Args:
        server_handle: Server handle

    Returns:
        True if successful
    """
    from datetime import datetime

    import httpx

    api_url = os.getenv("API_URL", "http://api:8000")

    try:
        async with httpx.AsyncClient() as client:
            # Get active incidents for this server
            resp = await client.get(
                f"{api_url}/api/incidents/",
                params={"server_handle": server_handle, "status": "detected"},
            )
            resp.raise_for_status()
            incidents = resp.json()

            # Also check for recovering status
            resp2 = await client.get(
                f"{api_url}/api/incidents/",
                params={"server_handle": server_handle, "status": "recovering"},
            )
            resp2.raise_for_status()
            incidents.extend(resp2.json())

            if not incidents:
                logger.debug(f"No active incidents to resolve for {server_handle}")
                return True

            resolved_at = datetime.utcnow().isoformat()

            for incident in incidents:
                incident_id = incident.get("id")
                await client.patch(
                    f"{api_url}/api/incidents/{incident_id}",
                    json={
                        "status": "resolved",
                        "resolved_at": resolved_at,
                    },
                )
                logger.info(f"Resolved incident #{incident_id} for server {server_handle}")

            return True

    except Exception as e:
        logger.error(f"Failed to resolve incidents for {server_handle}: {e}")
        return False


REINSTALL_TIMEOUT = int(os.getenv("REINSTALL_TIMEOUT", "900"))  # 15 minutes


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

        # Notify admin about long-running operation
        await notify_admins(
            f"⏳ Server *{server_handle}* OS reinstall started. This will take ~10-15 minutes.",
            level="info",
        )

        # Step 2: Wait for reinstall to complete and extract password
        task_result = await time4vps_client.wait_for_task(
            server_id=server_id,
            task_id=task_id,
            timeout=REINSTALL_TIMEOUT,
            poll_interval=15,  # Check every 15 seconds
        )

        logger.info(f"OS reinstall completed for {server_handle}")

        # Extract password from reinstall result
        # Reinstall returns password similarly to reset_password
        results = task_result.get("results", "")
        password = time4vps_client.extract_password(results)

        if not password:
            # Fallback: Try to reset password if we couldn't get it from reinstall details
            # (Though reinstall usually provides it)
            logger.warning(
                "Could not extract password from reinstall result. Attempting explicit password reset..."
            )
            password = await reset_server_password(time4vps_client, server_handle)

        if not password:
            return False, "Could not obtain root password after reinstall"

        # Step 3: Wait for server to boot
        import asyncio

        logger.info("Waiting 60s for server to fully boot...")
        await asyncio.sleep(60)

        # Step 4: Run Access Phase (Password + Key Injection)
        # Timeout: Short (120s is plenty)
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

        # Update labels to indicate progress
        await update_server_labels_in_db(
            server_handle, {"provisioning_phase": "software_installation"}
        )

        await notify_admins(
            f"✅ Server *{server_handle}* connectivity established. Starting software installation...",
            level="info",
        )

        # Step 5: Run Software Phase (Key Auth)
        # Timeout: Long (Server update)
        logger.info("Running Phase 2: Software Installation...")
        success_soft, output_soft = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_software.yml",
            root_password=None,  # Use keys now!
            timeout=PROVISIONING_TIMEOUT,  # 1200s
        )

        if success_soft:
            await update_server_labels_in_db(server_handle, {"provisioning_phase": "complete"})
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
    """Helper to handle successful provisioning."""
    await update_server_status_in_db(server_handle, "ready")

    recovery_text = "recovered and " if is_recovery else ""

    # Check for services to redeploy if this is incident recovery
    services_count = 0
    services_list_msg = ""

    if is_recovery:
        # Resolve any active incidents for this server
        await resolve_active_incidents(server_handle)
        
        logger.info(f"Checking for services to redeploy on {server_handle}")
        services = await get_services_on_server_for_redeployment(server_handle)
        services_count = len(services)

        if services_count > 0:
            logger.warning(
                f"Found {services_count} services that need redeployment on {server_handle}. "
                "Automatic redeployment not yet implemented - manual action required."
            )

            # Format list for notification
            services_list_msg = "Services needing redeployment:\n" + "\n".join(
                [f"• {s.get('service_name')} (port {s.get('port')})" for s in services[:5]]
            )
            if services_count > 5:
                services_list_msg += f"\n...and {services_count - 5} more."

            # Notify about services needing redeployment
            await notify_admins(
                f"⚠️ Server *{server_handle}* recovered, but {services_count} services need redeployment.\n\n"
                f"{services_list_msg}\n\n"
                "Please redeploy them manually.",
                level="warning",
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

    if is_recovery and services_count > 0:
        message += f"\n⚠️  {services_count} services need manual redeployment"

    # Send notification to admins
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
            "services_to_redeploy": services_count if is_recovery else 0,
        },
        "current_agent": "provisioner",
    }


async def run(state: dict) -> dict:
    """Run provisioner node.

    Orchestrates server provisioning:
    1. Reset password via Time4VPS
    2. Run Ansible playbook
    3. Update server status
    4. Handle errors and retries

    Args:
        state: Graph state

    Returns:
        Updated state with provisioning result
    """
    server_handle = state.get("server_to_provision")
    is_recovery = state.get("is_incident_recovery", False)

    if not server_handle:
        return {
            "messages": [AIMessage(content="⚠️ No server specified for provisioning")],
            "errors": state.get("errors", []) + ["No server_to_provision in state"],
        }

    # Get server info from API
    import httpx

    api_url = os.getenv("API_URL", "http://api:8000")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{api_url}/api/servers/{server_handle}")
            resp.raise_for_status()
            server_info = resp.json()
    except Exception as e:
        logger.error(f"Failed to get server info: {e}")
        return {
            "messages": [AIMessage(content=f"❌ Failed to get server info: {e}")],
            "errors": state.get("errors", []) + [f"Server info fetch failed: {e}"],
        }

    server_ip = server_info.get("public_ip") or server_info.get("host")
    server_status = server_info.get("status", "")
    os_template = server_info.get("os_template", "kvm-ubuntu-24.04-gpt-x86_64")
    provisioning_attempts = server_info.get("provisioning_attempts", 0)

    # Logic to determine action:
    # 1. Check if server is accessible via SSH Key
    # 2. If accessible -> Run Ansible directly (Skip Reinstall)
    # 3. If NOT accessible -> Trigger Reinstall -> Extract Password -> Run Ansible

    # Fetch server details to get IP and Time4VPS ID
    # The existing code already fetches server_info, so we can extract server_id from labels if present
    server_id = server_info.get("labels", {}).get("time4vps_id")
    if server_id:
        server_id = int(server_id)  # Ensure it's an int for Time4VPSClient

    if not server_ip:
        await update_server_status_in_db(server_handle, "error")
        return {
            "messages": [AIMessage(content=f"❌ Server {server_handle} has no public IP address.")],
            "errors": state.get("errors", []) + [f"Missing IP for {server_handle}"],
        }

    if not server_id:
        # Fallback to fetching server_id from Time4VPS API if not in labels
        time4vps_username = os.getenv("TIME4VPS_LOGIN") or os.getenv("TIME4VPS_USERNAME")
        time4vps_password = os.getenv("TIME4VPS_PASSWORD")
        if not time4vps_username or not time4vps_password:
            logger.error("TIME4VPS credentials not configured")
            await update_server_status_in_db(server_handle, "error")
            return {
                "messages": [AIMessage(content="❌ TIME4VPS credentials not configured")],
                "errors": state.get("errors", []) + ["Missing TIME4VPS credentials"],
            }
        time4vps_client_temp = Time4VPSClient(time4vps_username, time4vps_password)
        server_id = await get_server_id_from_time4vps(time4vps_client_temp, server_handle)
        if not server_id:
            await update_server_status_in_db(server_handle, "error")
            return {
                "messages": [AIMessage(content=f"❌ Server {server_handle} not found in Time4VPS")],
                "errors": state.get("errors", []) + ["Server not found in Time4VPS"],
            }

    # Check max attempts
    if provisioning_attempts >= PROVISIONING_MAX_RETRIES:
        await update_server_status_in_db(server_handle, "error")
        await create_incident_in_db(
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

    # Update status to PROVISIONING
    await update_server_status_in_db(server_handle, "provisioning")

    # Initialize Time4VPS client
    time4vps_username = os.getenv("TIME4VPS_LOGIN") or os.getenv("TIME4VPS_USERNAME")
    time4vps_password = os.getenv("TIME4VPS_PASSWORD")

    if not time4vps_username or not time4vps_password:
        logger.error("TIME4VPS credentials not configured")
        await update_server_status_in_db(server_handle, "error")
        return {
            "messages": [AIMessage(content="❌ TIME4VPS credentials not configured")],
            "errors": state.get("errors", []) + ["Missing TIME4VPS credentials"],
        }

    time4vps_client = Time4VPSClient(time4vps_username, time4vps_password)

    logger.info(f"Starting provisioning for {server_handle} (attempt {provisioning_attempts + 1})")

    use_reinstall = False
    if check_ssh_access(server_ip):
        logger.info(f"Server {server_handle} is accessible via SSH Key. Skipping reinstall.")
        use_reinstall = False
    else:
        logger.info(
            f"Server {server_handle} NOT accessible via SSH Key. Initiating Reinstall flow."
        )
        use_reinstall = True

    # Force reinstall override
    if state.get("force_reinstall") or server_status == "force_rebuild":
        logger.info(
            f"Force reinstall requested for {server_handle} or server status is force_rebuild."
        )
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
            await update_server_status_in_db(server_handle, "error")
            await create_incident_in_db(server_handle, "reinstall_failed", {"message": message})
            await notify_admins(
                f"❌ Server *{server_handle}* reinstall FAILED: {message[:200]}", level="error"
            )
            return {
                "messages": [AIMessage(content=f"❌ Reinstall failed: {message}")],
                "errors": state.get("errors", []) + ["Reinstall failed"],
                "provisioning_result": {"status": "failed", "method": "reinstall"},
            }

    # ===== EXISTING ACCESS PATH (SKIP REINSTALL) =====
    else:
        logger.info(f"Running provisioning playbooks on existing setup for {server_handle}")

        # Phase 1: Access
        # Even if we have access, we ensure keys/security are up to date.
        # We use Key Auth since we verified access via Key.
        success_access, output_access = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_access.yml",
            root_password=None,  # Use Key
            ssh_public_key=get_ssh_public_key(),
            timeout=180,
        )

        if not success_access:
            await update_server_status_in_db(server_handle, "error")
            await create_incident_in_db(
                server_handle,
                "provisioning_failed",
                {"step": "access_setup", "output": output_access[:500]},
            )
            return {
                "messages": [AIMessage(content=f"❌ Phase 1 (Access) failed for {server_handle}")],
                "errors": state.get("errors", []) + ["Phase 1 failed"],
            }

        await update_server_labels_in_db(
            server_handle, {"provisioning_phase": "software_installation"}
        )

        # Phase 2: Software
        success_soft, output_soft = run_ansible_playbook(
            server_ip=server_ip,
            server_handle=server_handle,
            playbook_name="provision_software.yml",
            root_password=None,  # Use Key
            timeout=PROVISIONING_TIMEOUT,
        )

        if success_soft:
            await update_server_labels_in_db(server_handle, {"provisioning_phase": "complete"})
            return await handle_provisioning_success(
                server_handle, server_ip, provisioning_attempts, is_recovery, " (Retried)"
            )
        else:
            await update_server_status_in_db(server_handle, "error")
            # Mark for force_rebuild if software fails? Or just error?
            # User prefers NO reinstall. So just error.
            await create_incident_in_db(
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
