"""Deploy capability tools for Dynamic ProductOwner.

Provides tools to trigger deployments, check status, and view logs.
Uses Redis queue for async job processing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from langchain_core.tools import tool
import structlog

from shared.queues import DEPLOY_QUEUE, get_user_active_jobs
from shared.redis_client import RedisStreamClient

# NOTE: get_current_state imported inside functions to avoid circular import
from .base import api_client

logger = structlog.get_logger(__name__)

# Max concurrent deploys per user
MAX_CONCURRENT_DEPLOYS = 3


@tool
async def check_deploy_readiness(
    project_id: Annotated[str, "Project ID to check readiness for"],
) -> dict:
    """Check if a project is ready for deployment.

    Verifies:
    - Project exists
    - Repository is configured
    - Resources allocated (server + port)
    - All required secrets configured

    Returns:
        {
            "ready": True/False,
            "missing": ["allocated_resources", "secrets"],
            "project_name": "...",
            "server": "vps-xxx",
            "port": 8080
        }
    """
    missing = []

    # 1. Get project
    project = await api_client.get_project(project_id)
    if not project:
        return {"ready": False, "missing": ["project_not_found"], "error": "Project not found"}

    project_name = project.get("name", project_id)

    # 2. Check repository
    repo_url = project.get("repository_url") or project.get("config", {}).get("repository_url")
    if not repo_url:
        missing.append("repository")

    # 3. Check allocated resources
    allocations = await api_client.get_project_allocations(project_id)
    if not allocations:
        missing.append("allocated_resources")

    # 4. Check required secrets
    config = project.get("config") or {}
    secrets = config.get("secrets") or {}
    required_secrets = config.get("required_secrets") or []

    missing_secrets = [s for s in required_secrets if s not in secrets]
    if missing_secrets:
        missing.append(f"secrets:{','.join(missing_secrets)}")

    allocation = allocations[0] if allocations else {}

    return {
        "ready": len(missing) == 0,
        "missing": missing,
        "project_name": project_name,
        "server": allocation.get("server_handle"),
        "port": allocation.get("port"),
    }


@tool
async def trigger_deploy(
    project_id: Annotated[str, "Project ID to deploy"],
) -> dict:
    """Start deployment for a project.

    Prerequisites (checked automatically):
    - Project exists and has repository
    - Resources allocated (server + port)
    - All required secrets configured

    After calling, use get_deploy_status(job_id) to monitor progress.

    Returns:
        {"job_id": "deploy_xxx", "status": "queued"}
        or {"error": "...", "missing": [...]} if not ready
    """
    from ..capabilities.base import get_current_state

    state = get_current_state()
    user_id = state.get("telegram_user_id")

    # 1. Check readiness
    readiness = await check_deploy_readiness.ainvoke({"project_id": project_id})
    if not readiness.get("ready"):
        return {
            "error": "Project not ready for deployment",
            "missing": readiness.get("missing", []),
        }

    # 2. Check concurrent deploy limit
    redis = RedisStreamClient()
    await redis.connect()

    try:
        active_jobs = await get_user_active_jobs(redis.redis, DEPLOY_QUEUE, user_id)
        if active_jobs >= MAX_CONCURRENT_DEPLOYS:
            msg = f"Too many concurrent deployments ({active_jobs}/{MAX_CONCURRENT_DEPLOYS})"
            return {
                "error": msg,
                "active_jobs": active_jobs,
            }

        # 3. Generate job_id
        job_id = f"deploy_{project_id}_{uuid4().hex[:8]}"

        # 4. Publish to queue
        await redis.publish(
            DEPLOY_QUEUE,
            {
                "job_id": job_id,
                "project_id": project_id,
                "user_id": str(user_id),
                "correlation_id": state.get("correlation_id", ""),
                "queued_at": datetime.now(UTC).isoformat(),
            },
        )

        logger.info(
            "deploy_queued",
            job_id=job_id,
            project_id=project_id,
            user_id=user_id,
        )

        return {
            "job_id": job_id,
            "status": "queued",
            "message": f"Deployment queued. Use get_deploy_status('{job_id}') to check progress.",
        }

    finally:
        await redis.close()


@tool
async def get_deploy_status(
    job_id: Annotated[str, "Job ID from trigger_deploy"],
) -> dict:
    """Check deployment progress.

    Args:
        job_id: Job ID returned by trigger_deploy

    Returns:
        {
            "status": "queued|running|success|failed",
            "progress": "Building Docker image...",
            "logs_tail": "...",
            "deployed_url": "http://...",  # if success
            "error": "...",                 # if failed
        }
    """
    # TODO: Read from LangGraph checkpointer when devops_subgraph is implemented
    # For now, check Redis for job data

    redis = RedisStreamClient()
    await redis.connect()

    try:
        # Check if job exists in stream (search recent entries)
        entries = await redis.redis.xrevrange(DEPLOY_QUEUE, count=100)

        for _entry_id, data in entries:
            if data.get("job_id") == job_id:
                return {
                    "status": data.get("status", "queued"),
                    "progress": data.get("progress", "Waiting in queue..."),
                    "logs_tail": data.get("logs_tail", ""),
                    "deployed_url": data.get("deployed_url"),
                    "error": data.get("error"),
                    "queued_at": data.get("queued_at"),
                }

        return {
            "status": "not_found",
            "error": f"No deployment with job_id={job_id}",
        }

    finally:
        await redis.close()


@tool
async def get_deploy_logs(
    job_id: Annotated[str, "Job ID from trigger_deploy"],
    lines: Annotated[int, "Number of log lines to return"] = 100,
) -> dict:
    """Get full deployment logs.

    Args:
        job_id: Job ID from trigger_deploy
        lines: Number of lines to return (default 100, max 1000)

    Returns:
        {"logs": "...", "status": "running|success|failed"}
    """
    lines = min(lines, 1000)

    # TODO: Read from LangGraph checkpointer when devops_subgraph is implemented
    status = await get_deploy_status.ainvoke({"job_id": job_id})

    if status.get("status") == "not_found":
        return {"error": f"No deployment with job_id={job_id}"}

    # For now return what we have
    logs = status.get("logs_tail", "")
    log_lines = logs.split("\n") if logs else []

    return {
        "logs": "\n".join(log_lines[-lines:]),
        "status": status.get("status", "unknown"),
        "total_lines": len(log_lines),
    }
