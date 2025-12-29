"""DevOps tools for deployment and secret management."""

import json
import os
import secrets
import subprocess
import tempfile
from typing import Annotated

from langchain_core.tools import tool
import structlog

from shared.clients.github import GitHubAppClient

from ..clients.api import api_client

logger = structlog.get_logger()

# SSH key path for deployment
SSH_KEY_PATH = "/root/.ssh/id_ed25519"


async def _create_service_deployment_record(
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


async def _setup_ci_secrets(
    github_client: GitHubAppClient,
    owner: str,
    repo: str,
    server_ip: str,
    project_name: str,
) -> bool:
    """Configure GitHub Actions secrets for CI/CD deployment."""
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

    secrets_map = {
        "DEPLOY_HOST": server_ip,
        "DEPLOY_USER": "root",
        "DEPLOY_SSH_KEY": ssh_key,
        "DEPLOY_PROJECT_PATH": f"/opt/services/{project_name}",
        "DEPLOY_COMPOSE_FILES": "infra/compose.base.yml infra/compose.prod.yml",
    }

    try:
        count = await github_client.set_repository_secrets(owner, repo, secrets_map)
        logger.info(
            "ci_secrets_configured",
            owner=owner,
            repo=repo,
            secrets_count=count,
            total=len(secrets_map),
        )
        return count == len(secrets_map)
    except Exception as e:
        logger.error(
            "ci_secrets_setup_failed",
            owner=owner,
            repo=repo,
            error=str(e),
            error_type=type(e).__name__,
        )
        return False


@tool
async def analyze_env_requirements(
    project_id: Annotated[str, "Project ID to analyze"],
) -> dict:
    """Analyze .env.example and classify each variable.

    Returns a dictionary with 'variables' list and their raw content.
    """
    logger.info("analyzing_env_requirements", project_id=project_id)

    project = await api_client.get_project(project_id)
    if not project:
        return {"error": "Project not found"}

    # Get repository info
    repo_url = project.get("repository_url") or project.get("config", {}).get("repository_url")
    if not repo_url:
        return {"error": "No repository URL found for project"}

    # Extract owner/repo from URL (assuming github.com/owner/repo)
    try:
        # url format: https://github.com/owner/repo
        parts = repo_url.rstrip("/").split("/")
        repo = parts[-1]
        owner = parts[-2]
    except Exception:
        return {"error": f"Invalid repository URL format: {repo_url}"}

    github = GitHubAppClient()

    try:
        content = await github.get_file_content(owner, repo, ".env.example")
        if not content:
            # Fallback to .env.template or just return empty if none
            content = await github.get_file_content(owner, repo, ".env.template")

        if not content:
            return {"variables": [], "message": "No .env.example found"}

        # Basic parsing
        variables = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            # Key=Value
            if "=" in line:
                key = line.split("=", 1)[0].strip()
                variables.append(key)

        return {"variables": variables, "raw_content": content}

    except Exception as e:
        logger.error("env_analysis_failed", error=str(e))
        return {"error": f"Failed to analyze env requirements: {e}"}


@tool
async def generate_infra_secret(
    key: Annotated[str, "Secret key name (e.g. DATABASE_URL)"],
    project_id: Annotated[str, "Project ID"],
) -> str:
    """Generate infrastructure secret value."""
    project_id = project_id.replace("-", "_").lower()

    # Get allocations to check for server existence
    # Get allocations to check for server existence (commented out as unused)
    # allocations = await api_client.get_project_allocations(project_id)

    # server_ip can be used if we need external access
    # if allocations:
    #     server_ip = allocations[0].get("server_ip", "127.0.0.1")

    if key == "REDIS_URL":
        # In our infra, redis is usually shared or local?
        # Assuming typical pattern: redis://redis:6379/0 or similar
        return "redis://redis:6379/0"

    elif key == "DATABASE_URL":
        # postgresql://user:pass@host:5432/db
        db_pass = secrets.token_urlsafe(16)
        db_name = f"db_{project_id}"
        # We assume we are inside docker network or using internal IP
        return f"postgresql://postgres:{db_pass}@postgres:5432/{db_name}"

    elif "SECRET" in key or "KEY" in key and "API" not in key:
        # Generic secret key
        return secrets.token_urlsafe(32)

    elif key == "POSTGRES_USER":
        return "postgres"
    elif key == "POSTGRES_PASSWORD":
        return secrets.token_urlsafe(16)
    elif key == "POSTGRES_DB":
        return f"db_{project_id}"

    # Default random for unknowns
    return secrets.token_urlsafe(32)


@tool
async def get_project_context(
    project_id: Annotated[str, "Project ID"],
) -> dict:
    """Get project context for computed secrets."""
    project = await api_client.get_project(project_id)
    if not project:
        return {"error": "Project not found"}

    return {
        "project_id": project.get("id"),
        "name": project.get("name"),
        "status": project.get("status"),
        "config": project.get("config"),
    }


@tool
async def run_ansible_deploy(
    project_id: Annotated[str, "Project ID"],
    secrets: Annotated[dict, "Secrets to inject into deployment"],
) -> dict:
    """Execute ansible deployment."""

    project = await api_client.get_project(project_id)
    if not project:
        return {"status": "failed", "error": "Project not found"}

    repo_url = project.get("repository_url") or project.get("config", {}).get("repository_url")
    if not repo_url:
        return {"status": "failed", "error": "No repository URL found"}

    try:
        parts = repo_url.rstrip("/").split("/")
        repo = parts[-1]
        owner = parts[-2]
    except Exception:
        return {"status": "failed", "error": "Invalid repository URL"}

    repo_full_name = f"{owner}/{repo}"
    project_name = project.get("name", "project").replace(" ", "_").lower()

    allocations = await api_client.get_project_allocations(project_id)
    if not allocations:
        return {"status": "failed", "error": "No resources allocated"}

    # Heuristic for deployment target
    target_resource = None
    for alloc in allocations:
        if alloc.get("port") and alloc.get("server_ip"):
            target_resource = alloc
            break

    if not target_resource:
        return {"status": "failed", "error": "No suitable allocation found (need port and IP)"}

    target_server_ip = target_resource.get("server_ip")
    target_port = target_resource.get("port")

    # Get GitHub token
    github_client = GitHubAppClient()
    try:
        token = await github_client.get_token(owner, repo)
    except Exception as e:
        return {"status": "failed", "error": f"Failed to get GitHub token: {e}"}

    # Prepare Ansible
    playbook_path = "/app/services/infrastructure/ansible/playbooks/deploy_project.yml"

    inventory_content = (
        f"{target_server_ip} ansible_user=root "
        "ansible_ssh_private_key_file=/root/.ssh/id_ed25519 "
        "ansible_ssh_common_args='-o StrictHostKeyChecking=no'"
    )

    with tempfile.NamedTemporaryFile(mode="w", delete=False) as inventory_file:
        inventory_file.write(inventory_content)
        inventory_path = inventory_file.name

    try:
        extra_vars_dict = {
            "project_name": project_name,
            "repo_full_name": repo_full_name,
            "github_token": token,
            "service_port": target_port,
        }

        # Merging secrets into extra_vars.
        # NOTE: Compatibility with existing playbook inferred from devops.py,
        # which explicitly passed 'telegram_token'.
        # We now pass all gathered secrets.
        # If playbook doesn't map them to env vars, this might need playbook update.
        # However, for Iteration 1, we assume best effort injection.

        for k, v in secrets.items():
            extra_vars_dict[k] = v

        # Also handle modules
        config = project.get("config") or {}
        if config.get("modules"):
            modules = config["modules"]
            if isinstance(modules, list):
                extra_vars_dict["selected_modules"] = ",".join(modules)
            else:
                extra_vars_dict["selected_modules"] = modules

        # Execute
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

        process = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if process.returncode == 0:
            deployed_url = f"http://{target_server_ip}:{target_port}"

            # Post-deployment: Create record and CI secrets
            await _create_service_deployment_record(
                project_id=project_id,
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

            await _setup_ci_secrets(
                github_client=github_client,
                owner=owner,
                repo=repo,
                server_ip=target_server_ip,
                project_name=project_name,
            )

            return {
                "status": "success",
                "deployed_url": deployed_url,
                "server_ip": target_server_ip,
                "port": target_port,
            }
        else:
            return {
                "status": "failed",
                "error": "Ansible playbook failed",
                "stdout": process.stdout,
                "stderr": process.stderr,
                "exit_code": process.returncode,
            }

    except Exception as e:
        logger.error("deployment_execution_failed", error=str(e), exc_info=True)
        return {"status": "error", "error": str(e)}

    finally:
        if os.path.exists(inventory_path):
            os.remove(inventory_path)
