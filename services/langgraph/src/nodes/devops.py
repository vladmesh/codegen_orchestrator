"""DevOps agent node.

Orchestrates the deployment of the application using Ansible.
Runs after the Developer agent/worker has completed implementation.
"""

import json
import os
import subprocess
import tempfile

from langchain_core.messages import AIMessage
import structlog

from shared.clients.github import GitHubAppClient
from src.clients.api import api_client

from .base import FunctionalNode, log_node_execution

logger = structlog.get_logger()


async def create_service_deployment_record(
    project_id: str,
    service_name: str,
    server_handle: str,
    port: int,
    deployment_info: dict,
) -> bool:
    """Create a service deployment record via API."""
    payload = {
        "project_id": project_id,
        "service_name": service_name,
        "server_handle": server_handle,
        "port": port,
        "status": "running",
        "deployment_info": deployment_info,
    }

    try:
        await api_client.create_service_deployment(payload)
        logger.info("service_deployment_record_created", service_name=service_name)
        return True
    except Exception as e:
        logger.error(
            "service_deployment_record_error",
            service_name=service_name,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


# SSH key path for deployment
SSH_KEY_PATH = "/root/.ssh/id_ed25519"


async def setup_ci_secrets(
    github_client: GitHubAppClient,
    owner: str,
    repo: str,
    server_ip: str,
    project_name: str,
) -> bool:
    """Configure GitHub Actions secrets for CI/CD deployment.

    Sets the following secrets:
    - DEPLOY_HOST: Server IP address
    - DEPLOY_USER: SSH user (root)
    - DEPLOY_SSH_KEY: SSH private key
    - DEPLOY_PROJECT_PATH: Path on server
    - DEPLOY_COMPOSE_FILES: Compose files to use

    Returns:
        True if all secrets were set successfully, False otherwise.
    """
    # Read SSH private key from mounted volume
    if not os.path.exists(SSH_KEY_PATH):
        logger.warning("ssh_key_not_found", path=SSH_KEY_PATH)
        return False

    try:
        with open(SSH_KEY_PATH) as f:
            ssh_key = f.read()
    except Exception as e:
        logger.error("ssh_key_read_failed", error=str(e))
        return False

    secrets = {
        "DEPLOY_HOST": server_ip,
        "DEPLOY_USER": "root",
        "DEPLOY_SSH_KEY": ssh_key,
        "DEPLOY_PROJECT_PATH": f"/opt/services/{project_name}",
        "DEPLOY_COMPOSE_FILES": "infra/compose.base.yml infra/compose.prod.yml",
    }

    try:
        count = await github_client.set_repository_secrets(owner, repo, secrets)
        logger.info(
            "ci_secrets_configured",
            owner=owner,
            repo=repo,
            secrets_count=count,
            total=len(secrets),
        )
        return count == len(secrets)
    except Exception as e:
        logger.error(
            "ci_secrets_setup_failed",
            owner=owner,
            repo=repo,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


class DevOpsNode(FunctionalNode):
    """DevOps agent node for deployment."""

    def __init__(self):
        super().__init__(node_id="devops")

    @log_node_execution("devops")
    async def run(self, state: dict) -> dict:
        """Run devops agent.

        1. Extract deployment info (repo, resources).
        2. Get GitHub token.
        3. Run Ansible playbook to deploy.
        4. Return status.
        """
        repo_info = state.get("repo_info") or {}
        project_spec = state.get("project_spec") or {}
        allocated_resources = state.get("allocated_resources") or {}

        if not repo_info:
            return {
                "errors": state.get("errors", []) + ["No repository info for deployment"],
                "messages": [AIMessage(content="❌ No repository info found. Cannot deploy.")],
            }

        # Identify the port and server from allocated resources
        # allocated_resources keys are like "server_handle:port"
        # value is dict with keys: port, server_handle, server_ip

        target_resource = None
        target_server_ip = None
        target_port = None

        # Simple heuristic: take the first allocated http/service resource
        # In reality we might filter by "is_web_service" or similar if we had that metadata
        for _, res in allocated_resources.items():
            if res.get("port") and res.get("server_ip"):
                target_resource = res
                target_server_ip = res.get("server_ip")
                target_port = res.get("port")
                break

        if not target_resource:
            return {
                "errors": state.get("errors", []) + ["No allocated server resource found"],
                "messages": [AIMessage(content="❌ No server resources allocated. Cannot deploy.")],
            }

        repo_full_name = repo_info.get("full_name")
        project_name = project_spec.get("name", "project").replace(" ", "_").lower()

        # Get token for deployment (pulling images, getting compose file)
        github_client = GitHubAppClient()
        owner, repo = repo_full_name.split("/")

        try:
            token = await github_client.get_token(owner, repo)
        except Exception as e:
            logger.error(
                "github_token_failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return {
                "errors": state.get("errors", []) + [f"Failed to get GitHub token: {e}"],
                "messages": [AIMessage(content=f"❌ Failed to get GitHub token: {e}")],
            }

        # Prepare Ansible Playbook execution
        playbook_path = "/app/services/infrastructure/ansible/playbooks/deploy_project.yml"

        # Construct inventory dynamically
        inventory_content = (
            f"{target_server_ip} ansible_user=root "
            "ansible_ssh_private_key_file=/root/.ssh/id_ed25519 "
            "ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
        )

        # Create temp inventory file
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as inventory_file:
            inventory_file.write(inventory_content)
            inventory_path = inventory_file.name

        # Build extra vars for Ansible
        extra_vars_dict = {
            "project_name": project_name,
            "repo_full_name": repo_full_name,
            "github_token": token,
            "service_port": target_port,
        }

        # Add telegram token if available (from project config secrets)
        config = project_spec.get("config") or {}
        secrets = config.get("secrets") or {}
        if secrets.get("telegram_token"):
            extra_vars_dict["telegram_token"] = secrets["telegram_token"]

        # Add selected modules if available
        if config.get("modules"):
            modules = config["modules"]
            if isinstance(modules, list):
                extra_vars_dict["selected_modules"] = ",".join(modules)
            else:
                extra_vars_dict["selected_modules"] = modules

        # Convert to JSON for safe passing of special characters in tokens
        extra_vars_json = json.dumps(extra_vars_dict)

        cmd = [
            "ansible-playbook",
            "-i",
            inventory_path,
            playbook_path,
            "--extra-vars",
            extra_vars_json,
        ]

        logger.info(
            "deployment_start",
            repo=repo_full_name,
            server_ip=target_server_ip,
            port=target_port,
        )

        try:
            # Run Ansible
            # Use simple subprocess, capturing output
            process = subprocess.run(  # noqa: S603
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes timeout
            )  # noqa: S603

            if process.returncode == 0:
                deployed_url = f"http://{target_server_ip}:{target_port}"

                # Record deployment for future recovery
                await create_service_deployment_record(
                    project_id=project_spec.get("id"),
                    service_name=project_name,
                    server_handle=target_resource.get("server_handle"),
                    port=target_port,
                    deployment_info={
                        "repo_full_name": repo_full_name,
                        "branch": "main",
                        "project_dir": f"/opt/services/{project_name}",
                        "compose_files": "infra/compose.base.yml infra/compose.prod.yml",
                        "modules": extra_vars_dict.get("selected_modules", "backend"),
                    },
                )

                # Configure GitHub Actions secrets for CI/CD
                ci_configured = await setup_ci_secrets(
                    github_client=github_client,
                    owner=owner,
                    repo=repo,
                    server_ip=target_server_ip,
                    project_name=project_name,
                )

                ci_status = "CI/CD configured" if ci_configured else "CI/CD setup failed"

                message = f"""✅ Deployment successful!

Project: {project_name}
URL: {deployed_url}
Server: {target_server_ip}
Port: {target_port}
{ci_status}
"""
                return {
                    "deployed_url": deployed_url,
                    "messages": [AIMessage(content=message)],
                    "current_agent": "devops",
                }
            else:
                logger.error(
                    "ansible_failed",
                    exit_code=process.returncode,
                    stderr=process.stderr[-500:] if process.stderr else None,
                )
                return {
                    "errors": state.get("errors", []) + ["Deployment failed"],
                    "messages": [
                        AIMessage(
                            content=(
                                f"❌ Deployment failed:\n\n{process.stderr[-500:]}\n\n"
                                f"STDOUT:\n{process.stdout[-200:]}"
                            )
                        )
                    ],
                    "current_agent": "devops",
                }

        except Exception as e:
            logger.error(
                "deployment_exception",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            return {
                "errors": state.get("errors", []) + [f"Deployment exception: {e}"],
                "messages": [AIMessage(content=f"❌ Deployment crashed: {e}")],
                "current_agent": "devops",
            }
        finally:
            # Cleanup inventory
            if os.path.exists(inventory_path):
                os.remove(inventory_path)


devops_node = DevOpsNode()
run = devops_node.run
