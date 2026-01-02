"""DevOps delegation tools for infrastructure-worker.

This module replaces direct Ansible execution with delegation to infrastructure-worker.
"""

import asyncio
import json
from typing import Annotated
import uuid

from langchain_core.tools import tool
import structlog

from shared.clients.github import GitHubAppClient
from shared.queues import ANSIBLE_DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient
from shared.schemas.deployment_jobs import DeploymentJobRequest, DeploymentJobResult

from ..clients.api import api_client
from ..schemas.api_types import AllocationInfo, ProjectInfo, ServerInfo, get_repo_url, get_server_ip

logger = structlog.get_logger()

# Polling configuration
RESULT_POLL_INTERVAL = 2  # seconds
RESULT_POLL_TIMEOUT = 300  # 5 minutes


async def _poll_deployment_result(
    redis: RedisStreamClient,
    request_id: str,
    timeout: int = RESULT_POLL_TIMEOUT,
) -> DeploymentJobResult | None:
    """Poll for deployment result from Redis.

    Args:
        redis: Redis client
        request_id: Request ID to poll for
        timeout: Maximum time to wait in seconds

    Returns:
        Deployment result or None if timeout
    """
    result_key = f"deploy:result:{request_id}"
    elapsed = 0

    while elapsed < timeout:
        result_json = await redis.redis.get(result_key)
        if result_json:
            result = json.loads(result_json)
            logger.info("deployment_result_received", request_id=request_id)
            return result

        await asyncio.sleep(RESULT_POLL_INTERVAL)
        elapsed += RESULT_POLL_INTERVAL

    logger.error("deployment_result_timeout", request_id=request_id, timeout=timeout)
    return None


@tool
async def delegate_ansible_deploy(
    project_id: Annotated[str, "Project ID"],
    secrets: Annotated[dict, "Resolved secrets to inject into deployment"],
) -> dict:
    """Delegate Ansible deployment to infrastructure-worker.

    This tool sends a deployment job to infrastructure-worker via Redis queue,
    then polls for the result. Post-deployment operations (service registration,
    CI secrets) are handled by the calling DeployerNode.

    Args:
        project_id: Project ID from database
        secrets: Resolved environment variables

    Returns:
        Deployment result with status, deployed_url, or error details
    """
    # Fetch project details
    project: ProjectInfo | None = await api_client.get_project(project_id)
    if not project:
        return {"status": "failed", "error": "Project not found"}

    repo_url = get_repo_url(project)
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

    # Get allocations
    allocations: list[AllocationInfo] = await api_client.get_project_allocations(project_id)
    if not allocations:
        return {"status": "failed", "error": "No resources allocated"}

    # Find allocation with port
    target_alloc: AllocationInfo | None = None
    for alloc in allocations:
        if alloc.get("port"):
            target_alloc = alloc
            break

    if not target_alloc:
        return {"status": "failed", "error": "No suitable allocation found (need port)"}

    target_port = target_alloc.get("port")

    # Get server_ip
    target_server_ip = target_alloc.get("server_ip")
    if not target_server_ip:
        server_handle = target_alloc.get("server_handle")
        if server_handle:
            server: ServerInfo | None = await api_client.get_server(server_handle)
            if server:
                target_server_ip = get_server_ip(server)

    if not target_server_ip:
        return {"status": "failed", "error": "Could not determine server IP"}

    # Get GitHub token
    github_client = GitHubAppClient()
    try:
        github_token = await github_client.get_token(owner, repo)
    except Exception as e:
        return {"status": "failed", "error": f"Failed to get GitHub token: {e}"}

    # Prepare deployment job
    request_id = str(uuid.uuid4())
    config = project.get("config") or {}
    modules = None
    if config.get("modules"):
        modules_value = config["modules"]
        if isinstance(modules_value, list):
            modules = ",".join(modules_value)
        else:
            modules = modules_value

    job: DeploymentJobRequest = {
        "job_type": "deploy",
        "request_id": request_id,
        "project_id": project_id,
        "project_name": project_name,
        "repo_full_name": repo_full_name,
        "github_token": github_token,
        "server_ip": target_server_ip,
        "port": target_port,
        "secrets": secrets,
        "modules": modules,
        "callback_stream": None,  # Not using callback stream for now
    }

    # Send to infrastructure-worker
    redis = RedisStreamClient()
    await redis.connect()

    try:
        await redis.redis.xadd(
            ANSIBLE_DEPLOY_QUEUE,
            {"data": json.dumps(job)},
        )
        logger.info(
            "deployment_job_submitted",
            request_id=request_id,
            project_id=project_id,
            repo=repo_full_name,
        )

        # Poll for result
        result = await _poll_deployment_result(redis, request_id)

        if result is None:
            return {
                "status": "failed",
                "error": "Deployment timeout - no response from infrastructure-worker",
            }

        return result

    except Exception as e:
        logger.error(
            "deployment_delegation_failed",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return {"status": "error", "error": str(e)}
    finally:
        await redis.close()
