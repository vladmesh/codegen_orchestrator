"""Deployment execution via Ansible for infrastructure-worker.

This module handles delegated deployment requests from langgraph DeployerNode.
It executes ansible-playbook for project deployment without containing
orchestration logic (which stays in langgraph).
"""

import json
import os
import subprocess
import tempfile

import structlog

logger = structlog.get_logger(__name__)

# Paths
ANSIBLE_PLAYBOOKS_PATH = "/app/ansible/playbooks"
SSH_KEY_PATH = "/root/.ssh/id_ed25519"


def run_deployment_playbook(
    project_name: str,
    repo_full_name: str,
    github_token: str,
    server_ip: str,
    service_port: int,
    secrets: dict[str, str],
    modules: str | None = None,
    timeout: int = 300,
) -> dict:
    """Execute Ansible deployment playbook.

    Args:
        project_name: Normalized project name (snake_case)
        repo_full_name: GitHub repo "owner/repo"
        github_token: GitHub token for cloning
        server_ip: Target server IP
        service_port: Allocated port
        secrets: Environment variables to inject
        modules: Optional comma-separated modules
        timeout: Execution timeout in seconds

    Returns:
        Result dict with status, deployed_url, or error details
    """
    playbook_path = f"{ANSIBLE_PLAYBOOKS_PATH}/deploy_project.yml"

    # Construct inventory
    inventory_content = (
        f"{server_ip} ansible_user=root "
        f"ansible_ssh_private_key_file={SSH_KEY_PATH} "
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
    )

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as inventory_file:
        inventory_file.write(inventory_content)
        inventory_path = inventory_file.name

    try:
        # Prepare extra vars
        extra_vars_dict = {
            "project_name": project_name,
            "repo_full_name": repo_full_name,
            "github_token": github_token,
            "service_port": service_port,
        }

        # Merge secrets
        for k, v in secrets.items():
            extra_vars_dict[k] = v

        # Add modules if specified
        if modules:
            # Ensure backend is included if tg_bot is present (dependency)
            module_list = [m.strip() for m in modules.split(",")]
            if "tg_bot" in module_list and "backend" not in module_list:
                module_list.insert(0, "backend")
            extra_vars_dict["selected_modules"] = ",".join(module_list)

        extra_vars_json = json.dumps(extra_vars_dict)

        # Build ansible-playbook command
        cmd = [
            "ansible-playbook",
            "-i",
            inventory_path,
            playbook_path,
            "--extra-vars",
            extra_vars_json,
        ]

        logger.info(
            "deployment_playbook_start",
            repo=repo_full_name,
            server_ip=server_ip,
            port=service_port,
        )

        # Execute
        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if process.returncode == 0:
            deployed_url = f"http://{server_ip}:{service_port}"
            logger.info(
                "deployment_playbook_success",
                repo=repo_full_name,
                deployed_url=deployed_url,
            )
            return {
                "status": "success",
                "deployed_url": deployed_url,
                "server_ip": server_ip,
                "port": service_port,
            }
        else:
            logger.error(
                "deployment_playbook_failed",
                exit_code=process.returncode,
                stdout=process.stdout[:2000] if process.stdout else None,
                stderr=process.stderr[:2000] if process.stderr else None,
            )
            return {
                "status": "failed",
                "error": "Ansible playbook failed",
                "stdout": process.stdout,
                "stderr": process.stderr,
                "exit_code": process.returncode,
            }

    except subprocess.TimeoutExpired:
        logger.error("deployment_playbook_timeout", timeout=timeout)
        return {
            "status": "failed",
            "error": f"Deployment timeout after {timeout}s",
        }
    except Exception as e:
        logger.error(
            "deployment_playbook_exception",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {
            "status": "error",
            "error": str(e),
        }
    finally:
        # Cleanup inventory file
        if os.path.exists(inventory_path):
            os.remove(inventory_path)
