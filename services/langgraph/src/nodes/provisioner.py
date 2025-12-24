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
from typing import Any

import httpx
from langchain_core.messages import AIMessage

from shared.notifications import notify_admins

from ..clients.time4vps import Time4VPSClient

logger = logging.getLogger(__name__)

# Configuration from environment
PROVISIONING_TIMEOUT = int(os.getenv("PROVISIONING_TIMEOUT", "600"))
PROVISIONING_MAX_RETRIES = int(os.getenv("PROVISIONING_MAX_RETRIES", "3"))
PASSWORD_RESET_TIMEOUT = int(os.getenv("PASSWORD_RESET_TIMEOUT", "300"))
PASSWORD_RESET_POLL_INTERVAL = int(os.getenv("PASSWORD_RESET_POLL_INTERVAL", "5"))


def get_ssh_public_key() -> str | None:
    """Read SSH public key from mounted ~/.ssh directory.
    
    Tries common key types in order of preference.
    
    Returns:
        Public key string or None if not found
    """
    key_paths = [
        "/root/.ssh/id_ed25519.pub",
        "/root/.ssh/id_rsa.pub",
        "/root/.ssh/id_ecdsa.pub",
    ]
    
    for path in key_paths:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    key = f.read().strip()
                    logger.info(f"Loaded SSH public key from {path}")
                    return key
            except Exception as e:
                logger.warning(f"Failed to read {path}: {e}")
    
    logger.error("No SSH public key found in /root/.ssh/")
    return None


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


async def get_server_id_from_time4vps(time4vps_client: Time4VPSClient, server_handle: str) -> int | None:
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
                
            srv_id = server.get('id') or server.get('server_id')
            if not srv_id:
                logger.warning(f"Server entry missing ID: {server}")
                continue
                
            # Match by handle (vps-{id})
            if f"vps-{srv_id}" == server_handle:
                return srv_id
                
        logger.error(f"Server {server_handle} not found in Time4VPS API (scanned {len(servers)} servers)")
        return None
    except Exception as e:
        logger.exception(f"Failed to get server ID: {e}")
        return None


async def reset_server_password(
    time4vps_client: Time4VPSClient,
    server_handle: str
) -> str | None:
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
            poll_interval=PASSWORD_RESET_POLL_INTERVAL
        )
        
        logger.info(f"Password reset completed for server {server_handle}")
        return password
        
    except TimeoutError as e:
        logger.error(f"Password reset timeout: {e}")
        return None
    except Exception as e:
        logger.exception(f"Password reset failed: {e}")
        return None


def run_provisioning_playbook(
    server_ip: str,
    root_password: str,
    server_handle: str
) -> tuple[bool, str]:
    """Run Ansible provisioning playbook.
    
    Args:
        server_ip: Server IP address
        root_password: Root password for SSH
        server_handle: Server handle for hostname
    
    Returns:
        Tuple of (success: bool, output: str)
    """
    playbook_path = "/app/services/infrastructure/ansible/playbooks/provision_server.yml"
    
    # Create temporary inventory with password auth
    inventory_content = f"""[target]
{server_ip} ansible_user=root ansible_ssh_pass={root_password} ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
"""
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as inv_file:
        inv_file.write(inventory_content)
        inventory_path = inv_file.name
    
    # Extra vars for playbook
    extra_vars = f"target_host={server_ip} server_hostname={server_handle}"
    
    # Construct ansible-playbook command
    cmd = [
        "ansible-playbook",
        "-i", inventory_path,
        playbook_path,
        "--extra-vars", extra_vars,
        "-v"  # Verbose for debugging
    ]
    
    logger.info(f"Running provisioning playbook for {server_handle} at {server_ip}")
    
    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROVISIONING_TIMEOUT
        )
        
        # Log output
        logger.info(f"Ansible stdout:\n{process.stdout}")
        if process.stderr:
            logger.warning(f"Ansible stderr:\n{process.stderr}")
        
        success = process.returncode == 0
        output = process.stdout if success else f"{process.stderr}\n\n{process.stdout}"
        
        return success, output
        
    except subprocess.TimeoutExpired:
        logger.error(f"Provisioning timeout after {PROVISIONING_TIMEOUT}s")
        return False, f"Timeout after {PROVISIONING_TIMEOUT}s"
    except Exception as e:
        logger.exception(f"Provisioning exception: {e}")
        return False, str(e)
    finally:
        # Cleanup
        if os.path.exists(inventory_path):
            os.remove(inventory_path)


def run_provisioning_playbook_with_key(
    server_ip: str,
    server_handle: str
) -> tuple[bool, str]:
    """Run Ansible provisioning playbook using SSH key authentication.
    
    Used after OS reinstall when SSH key was provided during reinstall.
    
    Args:
        server_ip: Server IP address
        server_handle: Server handle for hostname
    
    Returns:
        Tuple of (success: bool, output: str)
    """
    playbook_path = "/app/services/infrastructure/ansible/playbooks/provision_server.yml"
    
    # Create temporary inventory with key auth (uses default SSH key from ~/.ssh)
    inventory_content = f"""[target]
{server_ip} ansible_user=root ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
"""
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.ini') as inv_file:
        inv_file.write(inventory_content)
        inventory_path = inv_file.name
    
    # Extra vars for playbook
    extra_vars = f"target_host={server_ip} server_hostname={server_handle}"
    
    # Construct ansible-playbook command
    cmd = [
        "ansible-playbook",
        "-i", inventory_path,
        playbook_path,
        "--extra-vars", extra_vars,
        "-v"  # Verbose for debugging
    ]
    
    logger.info(f"Running provisioning playbook (key auth) for {server_handle} at {server_ip}")
    
    try:
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROVISIONING_TIMEOUT
        )
        
        # Log output
        logger.info(f"Ansible stdout:\n{process.stdout}")
        if process.stderr:
            logger.warning(f"Ansible stderr:\n{process.stderr}")
        
        success = process.returncode == 0
        output = process.stdout if success else f"{process.stderr}\n\n{process.stdout}"
        
        return success, output
        
    except subprocess.TimeoutExpired:
        logger.error(f"Provisioning timeout after {PROVISIONING_TIMEOUT}s")
        return False, f"Timeout after {PROVISIONING_TIMEOUT}s"
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
                f"{api_url}/api/servers/{server_handle}",
                json={"status": status}
            )
            resp.raise_for_status()
            logger.info(f"Updated server {server_handle} status to {status}")
            return True
    except Exception as e:
        logger.error(f"Failed to update server status: {e}")
        return False


async def create_incident_in_db(
    server_handle: str,
    incident_type: str,
    details: dict
) -> bool:
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
                    "affected_services": []
                }
            )
            resp.raise_for_status()
            logger.info(f"Created incident for server {server_handle}: {incident_type}")
            return True
    except Exception as e:
        logger.error(f"Failed to create incident: {e}")
        return False


REINSTALL_TIMEOUT = int(os.getenv("REINSTALL_TIMEOUT", "900"))  # 15 minutes


async def reinstall_and_provision(
    time4vps_client: Time4VPSClient,
    server_handle: str,
    server_id: int,
    server_ip: str,
    os_template: str
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
        # Step 1: Trigger reinstall (without ssh_key - it doesn't work reliably)
        task_id = await time4vps_client.reinstall_server(
            server_id=server_id,
            os_template=os_template
        )
        
        logger.info(f"Reinstall task created: {task_id}. Waiting for completion...")
        
        # Notify admin about long-running operation
        await notify_admins(
            f"⏳ Server *{server_handle}* OS reinstall started. "
            f"This will take ~10-15 minutes.",
            level="info"
        )
        
        # Step 2: Wait for reinstall to complete
        await time4vps_client.wait_for_task(
            server_id=server_id,
            task_id=task_id,
            timeout=REINSTALL_TIMEOUT,
            poll_interval=15  # Check every 15 seconds
        )
        
        logger.info(f"OS reinstall completed for {server_handle}")
        
        # Step 3: Wait for server to boot
        import asyncio
        logger.info("Waiting 60s for server to fully boot...")
        await asyncio.sleep(60)
        
        # Step 4: Reset password to get SSH access
        logger.info("Resetting password after reinstall...")
        password = await reset_server_password(time4vps_client, server_handle)
        
        if not password:
            return False, "Password reset failed after reinstall"
        
        # Step 5: Run Ansible with password
        success, output = run_provisioning_playbook(server_ip, password, server_handle)
        
        if success:
            return True, "OS reinstall and provisioning completed successfully"
        else:
            return False, f"Ansible failed after reinstall: {output[:500]}"
            
    except TimeoutError as e:
        logger.error(f"Reinstall timeout: {e}")
        return False, f"Reinstall timeout: {e}"
    except Exception as e:
        logger.exception(f"Reinstall failed: {e}")
        return False, f"Reinstall failed: {e}"


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
            "errors": state.get("errors", []) + ["No server_to_provision in state"]
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
            "errors": state.get("errors", []) + [f"Server info fetch failed: {e}"]
        }
    
    server_ip = server_info.get("public_ip") or server_info.get("host")
    server_status = server_info.get("status", "")
    os_template = server_info.get("os_template", "kvm-ubuntu-24.04-gpt-x86_64")
    provisioning_attempts = server_info.get("provisioning_attempts", 0)
    
    # Determine if we should use reinstall path
    use_reinstall = server_status == "force_rebuild" or state.get("force_reinstall", False)
    
    logger.info(f"Server {server_handle}: status={server_status}, use_reinstall={use_reinstall}")
    
    # Check max attempts
    if provisioning_attempts >= PROVISIONING_MAX_RETRIES:
        await update_server_status_in_db(server_handle, "error")
        await create_incident_in_db(
            server_handle,
            "provisioning_failed",
            {"reason": f"Max retries ({PROVISIONING_MAX_RETRIES}) exceeded"}
        )
        return {
            "messages": [AIMessage(content=f"❌ Max provisioning attempts ({PROVISIONING_MAX_RETRIES}) exceeded for {server_handle}")],
            "errors": state.get("errors", []) + ["Max provisioning attempts exceeded"]
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
            "errors": state.get("errors", []) + ["Missing TIME4VPS credentials"]
        }
    
    time4vps_client = Time4VPSClient(time4vps_username, time4vps_password)
    
    # Get Time4VPS server ID
    server_id = await get_server_id_from_time4vps(time4vps_client, server_handle)
    if not server_id:
        await update_server_status_in_db(server_handle, "error")
        return {
            "messages": [AIMessage(content=f"❌ Server {server_handle} not found in Time4VPS")],
            "errors": state.get("errors", []) + ["Server not found in Time4VPS"]
        }
    
    logger.info(f"Starting provisioning for {server_handle} (attempt {provisioning_attempts + 1}, reinstall={use_reinstall})")
    
    # ===== REINSTALL PATH =====
    if use_reinstall:
        success, message = await reinstall_and_provision(
            time4vps_client=time4vps_client,
            server_handle=server_handle,
            server_id=server_id,
            server_ip=server_ip,
            os_template=os_template
        )
        
        if success:
            await update_server_status_in_db(server_handle, "ready")
            await notify_admins(
                f"✅ Server *{server_handle}* reinstalled and provisioned successfully!",
                level="success"
            )
            return {
                "messages": [AIMessage(content=f"✅ {message}")],
                "provisioning_result": {"status": "success", "method": "reinstall"}
            }
        else:
            await update_server_status_in_db(server_handle, "error")
            await create_incident_in_db(server_handle, "reinstall_failed", {"message": message})
            await notify_admins(
                f"❌ Server *{server_handle}* reinstall FAILED: {message[:200]}",
                level="error"
            )
            return {
                "messages": [AIMessage(content=f"❌ Reinstall failed: {message}")],
                "errors": state.get("errors", []) + ["Reinstall failed"],
                "provisioning_result": {"status": "failed", "method": "reinstall"}
            }
    
    # ===== PASSWORD RESET PATH =====
    password = await reset_server_password(time4vps_client, server_handle)
    if not password:
        await update_server_status_in_db(server_handle, "error")
        await create_incident_in_db(
            server_handle,
            "provisioning_failed",
            {"step": "password_reset", "reason": "Password reset failed or timed out"}
        )
        return {
            "messages": [AIMessage(content=f"❌ Password reset failed for {server_handle}")],
            "errors": state.get("errors", []) + ["Password reset failed"],
            "provisioning_result": {"status": "failed", "step": "password_reset"}
        }
    
    # Step 2: Run Ansible playbook
    success, output = run_provisioning_playbook(server_ip, password, server_handle)
    
    if success:
        # Success! Update status to READY
        await update_server_status_in_db(server_handle, "ready")
        
        recovery_text = "recovered and " if is_recovery else ""
        
        # Check for services to redeploy if this is incident recovery
        services_count = 0
        services_list_msg = ""
        
        if is_recovery:
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
                    level="warning"
                )
        
        message = f"""✅ Server {server_handle} {recovery_text}provisioned successfully!
        
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
            level="success"
        )
        
        return {
            "messages": [AIMessage(content=message)],
            "provisioning_result": {
                "status": "success",
                "server_handle": server_handle,
                "server_ip": server_ip,
                "services_to_redeploy": services_count if is_recovery else 0
            },
            "current_agent": "provisioner"
        }
    else:
        # Failure - set force_rebuild for next attempt (full OS reinstall)
        # This handles cases where password auth is disabled
        await update_server_status_in_db(server_handle, "force_rebuild")
        await create_incident_in_db(
            server_handle,
            "provisioning_failed",
            {
                "step": "ansible_playbook",
                "attempt": provisioning_attempts + 1,
                "output": output[:500],  # Truncate
                "next_action": "force_rebuild"
            }
        )
        
        # Send notification to admins
        await notify_admins(
            f"⚠️ Provisioning FAILED for server *{server_handle}*. "
            f"Attempt {provisioning_attempts + 1}. "
            f"Server marked for *force_rebuild* (full OS reinstall) on next attempt.",
            level="warning"
        )
        
        return {
            "messages": [AIMessage(content=f"❌ Provisioning failed for {server_handle}. Server marked for force_rebuild.\n\n{output[:300]}")],
            "errors": state.get("errors", []) + ["Ansible playbook failed - escalating to force_rebuild"],
            "provisioning_result": {"status": "failed", "step": "ansible_playbook", "next_action": "force_rebuild"}
        }
