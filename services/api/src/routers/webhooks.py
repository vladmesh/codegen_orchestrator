"""GitHub webhook endpoint.

Receives workflow_run events from GitHub and triggers deploy jobs
when CI passes on main branch for active projects.
"""

import os
import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.models import Project, Repository, Run, User
from shared.queues import DEPLOY_QUEUE

from ..database import get_async_session
from ..utils.webhook_security import verify_github_signature

logger = structlog.get_logger()

router = APIRouter(tags=["webhooks"])


def _get_webhook_secret() -> str:
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("GITHUB_WEBHOOK_SECRET is not set")
    return secret


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header("", alias="X-Hub-Signature-256"),
    x_github_event: str = Header("", alias="X-GitHub-Event"),
    db: AsyncSession = Depends(get_async_session),
):
    """Handle GitHub webhook events.

    Filters for workflow_run events where CI succeeds on main branch,
    then publishes a deploy job for the matching project.
    """
    body = await request.body()

    # 1. Verify signature
    secret = _get_webhook_secret()
    if not verify_github_signature(body, x_hub_signature_256, secret):
        return Response(
            content='{"detail": "Invalid signature"}',
            status_code=status.HTTP_401_UNAUTHORIZED,
            media_type="application/json",
        )

    # 2. Filter: only workflow_run events
    if x_github_event != "workflow_run":
        return {"status": "ignored", "reason": f"event type: {x_github_event}"}

    import json

    payload = json.loads(body)

    # 3. Filter: only completed actions
    if payload.get("action") != "completed":
        return {"status": "ignored", "reason": f"action: {payload.get('action')}"}

    workflow_run = payload.get("workflow_run", {})

    # 4. Filter: only successful conclusions
    if workflow_run.get("conclusion") != "success":
        return {"status": "ignored", "reason": f"conclusion: {workflow_run.get('conclusion')}"}

    # 5. Filter: only ci.yml (ignore deploy.yml to prevent loops)
    workflow_path = workflow_run.get("path", "")
    if not workflow_path.endswith("ci.yml"):
        return {"status": "ignored", "reason": f"workflow: {workflow_path}"}

    # 6. Filter: only main branch
    if workflow_run.get("head_branch") != "main":
        return {
            "status": "ignored",
            "reason": f"branch: {workflow_run.get('head_branch')}",
        }

    # 7. Lookup project via Repository.provider_repo_id
    repo_id = payload.get("repository", {}).get("id")
    if not repo_id:
        return {"status": "ignored", "reason": "no repository.id"}

    repo_query = select(Repository).where(Repository.provider_repo_id == repo_id)
    repo_result = await db.execute(repo_query)
    repo = repo_result.scalar_one_or_none()

    if not repo:
        logger.debug("webhook_unknown_repo", repo_id=repo_id)
        return {"status": "ignored", "reason": "unknown repository"}

    project = await db.get(Project, repo.project_id)
    if not project:
        logger.debug("webhook_repo_orphaned", repo_id=repo_id)
        return {"status": "ignored", "reason": "unknown repository"}

    # 8. Guard: project must be active
    if project.status != "active":
        logger.info(
            "webhook_skip_non_active",
            project_id=project.id,
            status=project.status,
        )
        return {"status": "ignored", "reason": f"project status: {project.status}"}

    # 9. Lookup owner for telegram_id (owner always exists)
    owner_query = select(User).where(User.id == project.owner_id)
    owner_result = await db.execute(owner_query)
    owner = owner_result.scalar_one_or_none()
    telegram_id = owner.telegram_id if owner else None

    # 10. Create Run record
    head_sha = workflow_run.get("head_sha", "")
    run_id = f"deploy-wh-{uuid.uuid4().hex[:8]}"

    db_run = Run(
        id=run_id,
        type="deploy",
        status="queued",
        project_id=project.id,
        user_id=owner.id if owner else None,
        run_metadata={"triggered_by": "webhook", "head_sha": head_sha},
    )
    db.add(db_run)
    await db.commit()

    logger.info(
        "webhook_deploy_triggered",
        run_id=run_id,
        project_id=project.id,
        head_sha=head_sha[:7] if head_sha else "",
    )

    # 11. Publish to deploy:queue
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is not set")

    deploy_msg = DeployMessage(
        task_id=run_id,
        project_id=str(project.id),
        user_id=str(telegram_id or ""),
        triggered_by=DeployTrigger.WEBHOOK,
    )
    r = aioredis.from_url(redis_url)
    try:
        await r.xadd(
            DEPLOY_QUEUE,
            {"data": deploy_msg.model_dump_json()},
        )
    finally:
        await r.aclose()

    return {"status": "accepted", "run_id": run_id, "project_id": project.id}
