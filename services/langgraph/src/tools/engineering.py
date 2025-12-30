"""Engineering capability tools for Dynamic ProductOwner.

Provides tools to trigger code implementation pipeline and monitor progress.
Phase 4.3 addition.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from langchain_core.tools import tool
import structlog

from shared.queues import ENGINEERING_QUEUE, get_user_active_jobs
from shared.redis_client import RedisStreamClient

from ..state.context import get_current_state
from .base import api_client

logger = structlog.get_logger(__name__)

# Max concurrent engineering jobs per user
MAX_CONCURRENT_ENGINEERING = 2


@tool
async def trigger_engineering(
    project_id: Annotated[str, "Project ID to work on"],
    task_description: Annotated[str, "What to implement (feature, fix, etc)"],
) -> dict:
    """Trigger code implementation pipeline (Analyst → Developer → Tester).

    Starts an async engineering job that will:
    1. Analyze the task requirements
    2. Implement the code changes
    3. Run tests and create a PR

    After calling, use get_engineering_status(job_id) to monitor progress.

    Args:
        project_id: Project to work on
        task_description: What to implement

    Returns:
        {"job_id": "eng_xxx", "status": "queued"}
    """
    state = get_current_state()
    user_id = state.get("telegram_user_id")

    # Check project exists
    project = await api_client.get_project(project_id)
    if not project:
        return {"error": f"Project {project_id} not found"}

    # Check concurrent job limit
    redis = RedisStreamClient()
    await redis.connect()

    try:
        active_jobs = await get_user_active_jobs(redis.redis, ENGINEERING_QUEUE, user_id)
        if active_jobs >= MAX_CONCURRENT_ENGINEERING:
            msg = (
                f"Too many concurrent engineering jobs ({active_jobs}/{MAX_CONCURRENT_ENGINEERING})"
            )
            return {
                "error": msg,
                "active_jobs": active_jobs,
            }

        # Generate job_id
        job_id = f"eng_{project_id}_{uuid4().hex[:8]}"

        # Publish to queue
        await redis.publish(
            ENGINEERING_QUEUE,
            {
                "job_id": job_id,
                "project_id": project_id,
                "task_description": task_description,
                "user_id": str(user_id),
                "correlation_id": state.get("correlation_id", ""),
                "queued_at": datetime.now(UTC).isoformat(),
            },
        )

        logger.info(
            "engineering_queued",
            job_id=job_id,
            project_id=project_id,
            task_description=task_description[:100],
        )

        return {
            "job_id": job_id,
            "status": "queued",
            "message": f"Engineering task queued. Use get_engineering_status('{job_id}') to check.",
        }

    finally:
        await redis.close()


@tool
async def get_engineering_status(
    job_id: Annotated[str, "Job ID from trigger_engineering"],
) -> dict:
    """Check engineering pipeline progress.

    Returns:
        {
            "status": "queued|analyzing|implementing|testing|success|failed",
            "current_stage": "Developer",
            "iterations": 2,
            "pr_url": "https://github.com/...",  # if success
            "error": "...",                       # if failed
        }
    """
    redis = RedisStreamClient()
    await redis.connect()

    try:
        # Search for job in stream
        entries = await redis.redis.xrevrange(ENGINEERING_QUEUE, count=100)

        for _entry_id, data in entries:
            if data.get("job_id") == job_id:
                return {
                    "status": data.get("status", "queued"),
                    "current_stage": data.get("current_stage", "Waiting"),
                    "iterations": data.get("iterations", 0),
                    "pr_url": data.get("pr_url"),
                    "error": data.get("error"),
                    "queued_at": data.get("queued_at"),
                    "task_description": data.get("task_description"),
                }

        return {
            "status": "not_found",
            "error": f"No engineering job with job_id={job_id}",
        }

    finally:
        await redis.close()


@tool
async def view_latest_pr(
    project_id: Annotated[str, "Project ID to check PRs for"],
) -> dict:
    """Get latest PR created for a project.

    Returns:
        {
            "pr_url": "https://github.com/...",
            "title": "Add feature X",
            "status": "open|merged|closed",
            "created_at": "...",
        }
    """
    from ..tools.github import get_github_client

    # Get project to find repo
    project = await api_client.get_project(project_id)
    if not project:
        return {"error": f"Project {project_id} not found"}

    repo_url = project.get("repository_url", "")
    if not repo_url:
        return {"error": "Project has no repository configured"}

    # Parse owner/repo from URL
    # Format: https://github.com/owner/repo or git@github.com:owner/repo.git
    try:
        if "github.com" in repo_url:
            parts = repo_url.replace("https://github.com/", "").replace("git@github.com:", "")
            parts = parts.rstrip(".git").split("/")
            min_url_parts = 2
            if len(parts) >= min_url_parts:
                owner, repo = parts[0], parts[1]
            else:
                return {"error": f"Could not parse repository URL: {repo_url}"}
        else:
            return {"error": f"Unsupported repository URL: {repo_url}"}

        github = get_github_client()

        # Get latest PR
        prs = await github.list_pull_requests(owner, repo, state="all", per_page=1)

        if not prs:
            return {"error": "No PRs found for this project", "project_id": project_id}

        pr = prs[0]
        return {
            "pr_url": pr.get("html_url"),
            "title": pr.get("title"),
            "status": pr.get("state"),
            "created_at": pr.get("created_at"),
            "author": pr.get("user", {}).get("login"),
            "number": pr.get("number"),
        }

    except Exception as e:
        logger.error("view_latest_pr_error", error=str(e), project_id=project_id)
        return {"error": str(e)}
