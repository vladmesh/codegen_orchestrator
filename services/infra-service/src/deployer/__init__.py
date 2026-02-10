"""Deployer module for handling ansible:deploy:queue jobs.

This module handles new project deployments using Ansible.
Reuses similar logic to recovery.py but for fresh deployments.
"""

import os
import subprocess
import tempfile

import structlog

from ..config.constants import Paths, Timeouts

logger = structlog.get_logger(__name__)


async def deploy_project(
    project_name: str,
    repo_full_name: str,
    github_token: str,
    server_ip: str,
    port: int,
    secrets: dict[str, str],
    modules: str | None = None,
) -> tuple[bool, str]:
    """Deploy a project to a server using Ansible.

    Args:
        project_name: Name of the project
        repo_full_name: Full GitHub repo name (owner/repo)
        github_token: GitHub token for repo access
        server_ip: Target server IP address
        port: Port to deploy on
        secrets: Environment secrets dict
        modules: Comma-separated list of modules (e.g., "backend,tg_bot")

    Returns:
        Tuple of (success: bool, message: str)
    """
    playbook_path = Paths.playbook("deploy_project.yml")

    # Construct inventory
    inventory_content = (
        f"{server_ip} ansible_user=root ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
    )

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".ini") as inv_file:
        inv_file.write(inventory_content)
        inventory_path = inv_file.name

    # Write secrets to temp file for Ansible
    secrets_file_path = None
    if secrets:
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".env") as secrets_file:
            for key, value in secrets.items():
                secrets_file.write(f"{key}={value}\n")
            secrets_file_path = secrets_file.name

    # Build extra vars
    extra_vars = (
        f"project_name={project_name} "
        f"repo_full_name={repo_full_name} "
        f"github_token={github_token} "
        f"service_port={port}"
    )

    if secrets_file_path:
        extra_vars += f" env_file={secrets_file_path}"

    if modules:
        extra_vars += f" modules={modules}"

    cmd = ["ansible-playbook", "-i", inventory_path, playbook_path, "--extra-vars", extra_vars]

    logger.info(
        "project_deployment",
        project_name=project_name,
        server_ip=server_ip,
        port=port,
        status="start",
    )

    try:
        process = subprocess.run(
            cmd, capture_output=True, text=True, timeout=Timeouts.SERVICE_DEPLOY
        )

        if process.returncode == 0:
            logger.info(
                "project_deployment",
                project_name=project_name,
                server_ip=server_ip,
                port=port,
                status="success",
            )
            return True, f"Project {project_name} deployed successfully to {server_ip}:{port}"
        else:
            # Capture both stdout and stderr for debugging
            error_output = ""
            if process.stderr:
                error_output = process.stderr[-1500:]
            elif process.stdout:
                error_output = process.stdout[-1500:]
            else:
                error_output = f"Exit code: {process.returncode}"

            logger.error(
                "project_deployment",
                project_name=project_name,
                server_ip=server_ip,
                port=port,
                status="failed",
                exit_code=process.returncode,
                stderr=process.stderr[-500:] if process.stderr else None,
                stdout=process.stdout[-500:] if process.stdout else None,
            )
            return False, f"Ansible failed: {error_output}"

    except subprocess.TimeoutExpired:
        logger.error(
            "project_deployment",
            project_name=project_name,
            server_ip=server_ip,
            port=port,
            status="timeout",
        )
        return False, f"Deployment timeout for {project_name}"
    except Exception as e:
        logger.error(
            "project_deployment",
            project_name=project_name,
            server_ip=server_ip,
            port=port,
            status="error",
            error=str(e),
            error_type=type(e).__name__,
        )
        return False, f"Deployment error: {e}"
    finally:
        if os.path.exists(inventory_path):
            os.remove(inventory_path)
        if secrets_file_path and os.path.exists(secrets_file_path):
            os.remove(secrets_file_path)
