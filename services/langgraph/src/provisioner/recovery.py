"""Service recovery for provisioner - redeploys services after server recovery."""

import logging
import os
import subprocess
import tempfile

from shared.notifications import notify_admins

from .api_client import get_services_on_server

logger = logging.getLogger(__name__)

MAX_ERROR_PREVIEW = 5


async def redeploy_service(
    service: dict,
    server_ip: str,
    github_token: str,
) -> tuple[bool, str]:
    """Redeploy a single service to the recovered server.

    Args:
        service: Service deployment record from API
        server_ip: Server IP address
        github_token: GitHub token for repo access

    Returns:
        Tuple of (success: bool, message: str)
    """
    service_name = service.get("service_name", "unknown")
    repo_full_name = service.get("deployment_info", {}).get("repo_full_name")
    port = service.get("port")

    if not repo_full_name:
        return False, f"Service {service_name} has no repo_full_name in deployment_info"

    if not port:
        return False, f"Service {service_name} has no port"

    playbook_path = "/app/services/infrastructure/ansible/playbooks/deploy_project.yml"

    # Construct inventory
    inventory_content = (
        f"{server_ip} ansible_user=root ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
    )

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".ini") as inv_file:
        inv_file.write(inventory_content)
        inventory_path = inv_file.name

    extra_vars = (
        f"project_name={service_name} "
        f"repo_full_name={repo_full_name} "
        f"github_token={github_token} "
        f"service_port={port}"
    )

    cmd = ["ansible-playbook", "-i", inventory_path, playbook_path, "--extra-vars", extra_vars]

    logger.info(f"Redeploying {service_name} to {server_ip}:{port}")

    try:
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if process.returncode == 0:
            return True, f"Service {service_name} redeployed successfully"
        else:
            return False, f"Ansible failed: {process.stderr[-500:]}"

    except subprocess.TimeoutExpired:
        return False, f"Deployment timeout for {service_name}"
    except Exception as e:
        return False, f"Deployment error: {e}"
    finally:
        if os.path.exists(inventory_path):
            os.remove(inventory_path)


async def redeploy_all_services(
    server_handle: str,
    server_ip: str,
) -> tuple[int, int, list[str]]:
    """Redeploy all services on a recovered server.

    Args:
        server_handle: Server handle
        server_ip: Server IP address

    Returns:
        Tuple of (success_count, fail_count, error_messages)
    """
    from ..clients.github import GitHubAppClient

    services = await get_services_on_server(server_handle)

    if not services:
        logger.info(f"No services to redeploy on {server_handle}")
        return 0, 0, []

    logger.info(f"Found {len(services)} services to redeploy on {server_handle}")

    # Get GitHub token
    github_client = GitHubAppClient()
    success_count = 0
    fail_count = 0
    errors = []

    for service in services:
        service_name = service.get("service_name", "unknown")
        repo_full_name = service.get("deployment_info", {}).get("repo_full_name")

        if not repo_full_name:
            errors.append(f"{service_name}: no repo info")
            fail_count += 1
            continue

        try:
            owner, repo = repo_full_name.split("/")
            token = await github_client.get_token(owner, repo)
        except Exception as e:
            errors.append(f"{service_name}: failed to get token - {e}")
            fail_count += 1
            continue

        success, message = await redeploy_service(service, server_ip, token)

        if success:
            success_count += 1
            logger.info(f"✅ {message}")
        else:
            fail_count += 1
            errors.append(f"{service_name}: {message}")
            logger.error(f"❌ {message}")

    # Notify about results
    if fail_count == 0 and success_count > 0:
        await notify_admins(
            f"✅ All {success_count} services redeployed on *{server_handle}*",
            level="success",
        )
    elif fail_count > 0:
        error_summary = "\n".join(errors[:MAX_ERROR_PREVIEW])
        if len(errors) > MAX_ERROR_PREVIEW:
            error_summary += f"\n...and {len(errors) - MAX_ERROR_PREVIEW} more"

        await notify_admins(
            f"⚠️ Service redeployment on *{server_handle}*:\n"
            f"✅ {success_count} succeeded\n"
            f"❌ {fail_count} failed\n\n"
            f"Errors:\n{error_summary}",
            level="warning",
        )

    return success_count, fail_count, errors
