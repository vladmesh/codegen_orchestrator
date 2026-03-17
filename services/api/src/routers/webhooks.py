"""GitHub webhook endpoint.

Handles:
- workflow_run (CI success on main) → trigger deploy
- workflow_run (CI failure on story/* branch) → create fix task, reopen story
- pull_request (merged story/* → main) → trigger deploy
"""

import json
import os
import uuid

from fastapi import APIRouter, Depends, Header, Request, Response, status
import httpx
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.run import RunStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.models import Project, Repository, Run, Story, User
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


async def _lookup_repo_and_project(
    db: AsyncSession, repo_id: int
) -> tuple[Repository | None, Project | None]:
    """Look up Repository and Project by GitHub provider_repo_id."""
    repo_query = select(Repository).where(Repository.provider_repo_id == repo_id)
    repo_result = await db.execute(repo_query)
    repo = repo_result.scalar_one_or_none()
    if not repo:
        return None, None

    project = await db.get(Project, repo.project_id)
    return repo, project


async def _publish_deploy(
    project: Project,
    db: AsyncSession,
    head_sha: str = "",
    story_id: str = "",
) -> dict:
    """Create Run record and publish DeployMessage. Returns response dict."""
    # Lookup owner for telegram_id
    owner_query = select(User).where(User.id == project.owner_id)
    owner_result = await db.execute(owner_query)
    owner = owner_result.scalar_one_or_none()
    telegram_id = owner.telegram_id if owner else None

    run_id = f"deploy-wh-{uuid.uuid4().hex[:8]}"
    db_run = Run(
        id=run_id,
        type="deploy",
        status=RunStatus.QUEUED.value,
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
        story_id=story_id,
    )

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL is not set")

    deploy_msg = DeployMessage(
        task_id=run_id,
        project_id=str(project.id),
        user_id=str(telegram_id or ""),
        story_id=story_id,
        triggered_by=DeployTrigger.WEBHOOK,
        action="feature",
    )
    r = aioredis.from_url(redis_url)
    try:
        await r.xadd(DEPLOY_QUEUE, {"data": deploy_msg.model_dump_json()})
    finally:
        await r.aclose()

    return {"status": "accepted", "run_id": run_id, "project_id": project.id, "story_id": story_id}


def _get_api_url() -> str:
    api_url = os.getenv("API_URL")
    if not api_url:
        raise RuntimeError("API_URL is not set")
    return api_url


async def _transition_story_via_api(story_id: str, action: str) -> None:
    """Transition story status via internal API call."""
    api_url = _get_api_url()
    async with httpx.AsyncClient() as client:
        await client.post(f"{api_url}/api/stories/{story_id}/{action}", timeout=10)


@router.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header("", alias="X-Hub-Signature-256"),
    x_github_event: str = Header("", alias="X-GitHub-Event"),
    db: AsyncSession = Depends(get_async_session),
):
    """Handle GitHub webhook events."""
    body = await request.body()

    # 1. Verify signature
    secret = _get_webhook_secret()
    if not verify_github_signature(body, x_hub_signature_256, secret):
        return Response(
            content='{"detail": "Invalid signature"}',
            status_code=status.HTTP_401_UNAUTHORIZED,
            media_type="application/json",
        )

    # 2. Route by event type
    if x_github_event == "pull_request":
        return await _handle_pull_request(body, db)
    elif x_github_event == "workflow_run":
        return await _handle_workflow_run(body, db)
    else:
        return {"status": "ignored", "reason": f"event type: {x_github_event}"}


async def _handle_pull_request(body: bytes, db: AsyncSession) -> dict:
    """Handle pull_request events — detect story branch merges to main."""
    payload = json.loads(body)

    # Only care about closed (merged) PRs
    if payload.get("action") != "closed":
        return {"status": "ignored", "reason": f"action: {payload.get('action')}"}

    pr = payload.get("pull_request", {})
    if not pr.get("merged"):
        return {"status": "ignored", "reason": "not merged"}

    head_ref = pr.get("head", {}).get("ref", "")
    base_ref = pr.get("base", {}).get("ref", "")

    # Only story/* branches merged to main
    if base_ref != "main":
        return {"status": "ignored", "reason": f"base: {base_ref}"}
    if not head_ref.startswith("story/"):
        return {"status": "ignored", "reason": f"non-story branch: {head_ref}"}

    story_id = head_ref.removeprefix("story/")

    # Lookup repo and project
    repo_id = payload.get("repository", {}).get("id")
    if not repo_id:
        return {"status": "ignored", "reason": "no repository.id"}

    repo, project = await _lookup_repo_and_project(db, repo_id)
    if not repo or not project:
        logger.debug("webhook_pr_unknown_repo", repo_id=repo_id)
        return {"status": "ignored", "reason": "unknown repository"}

    if project.status != ProjectStatus.ACTIVE:
        return {"status": "ignored", "reason": f"project status: {project.status}"}

    # Verify story exists
    story_query = select(Story).where(Story.id == story_id)
    story_result = await db.execute(story_query)
    story = story_result.scalar_one_or_none()
    if not story:
        logger.warning("webhook_pr_story_not_found", story_id=story_id)
        return {"status": "ignored", "reason": f"story not found: {story_id}"}

    # Transition story to deploying
    await _transition_story_via_api(story_id, "deploy")
    logger.info("webhook_pr_merged_deploy", story_id=story_id, pr_number=pr.get("number"))

    head_sha = pr.get("head", {}).get("sha", "")
    return await _publish_deploy(project, db, head_sha=head_sha, story_id=story_id)


async def _handle_workflow_run(body: bytes, db: AsyncSession) -> dict:
    """Handle workflow_run events — CI success on main or CI failure on story branch."""
    payload = json.loads(body)

    if payload.get("action") != "completed":
        return {"status": "ignored", "reason": f"action: {payload.get('action')}"}

    workflow_run = payload.get("workflow_run", {})
    workflow_path = workflow_run.get("path", "")

    # Only ci.yml (ignore deploy.yml to prevent loops)
    if not workflow_path.endswith("ci.yml"):
        return {"status": "ignored", "reason": f"workflow: {workflow_path}"}

    conclusion = workflow_run.get("conclusion")
    head_branch = workflow_run.get("head_branch", "")

    # CI failure on story/* branch → create fix task
    if conclusion == "failure" and head_branch.startswith("story/"):
        return await _handle_ci_failure_on_story(payload, workflow_run, db)

    # CI success on main → trigger deploy (existing behavior)
    if conclusion == "success" and head_branch == "main":
        return await _handle_ci_success_on_main(payload, workflow_run, db)

    # Everything else: ignored
    return {
        "status": "ignored",
        "reason": f"conclusion: {conclusion}, branch: {head_branch}",
    }


async def _handle_ci_success_on_main(payload: dict, workflow_run: dict, db: AsyncSession) -> dict:
    """CI passed on main branch — log only, deploy is triggered by PR merge event."""
    repo_id = payload.get("repository", {}).get("id")
    if not repo_id:
        return {"status": "ignored", "reason": "no repository.id"}

    repo, project = await _lookup_repo_and_project(db, repo_id)
    if not repo or not project:
        logger.debug("webhook_unknown_repo", repo_id=repo_id)
        return {"status": "ignored", "reason": "unknown repository"}

    head_sha = workflow_run.get("head_sha", "")
    logger.info(
        "webhook_ci_success_on_main",
        project_id=project.id,
        head_sha=head_sha[:7] if head_sha else "",
    )
    return {"status": "ignored", "reason": "deploy triggered by PR merge, not CI success"}


async def _handle_ci_failure_on_story(payload: dict, workflow_run: dict, db: AsyncSession) -> dict:
    """CI failed on story/* branch → create fix task, transition story back to in_progress."""
    head_branch = workflow_run.get("head_branch", "")
    story_id = head_branch.removeprefix("story/")

    repo_id = payload.get("repository", {}).get("id")
    if not repo_id:
        return {"status": "ignored", "reason": "no repository.id"}

    repo, project = await _lookup_repo_and_project(db, repo_id)
    if not repo or not project:
        return {"status": "ignored", "reason": "unknown repository"}

    # Verify story exists
    story_query = select(Story).where(Story.id == story_id)
    story_result = await db.execute(story_query)
    story = story_result.scalar_one_or_none()
    if not story:
        logger.warning("webhook_ci_fail_story_not_found", story_id=story_id)
        return {"status": "ignored", "reason": f"story not found: {story_id}"}

    run_url = workflow_run.get("html_url", "")
    run_id = workflow_run.get("id", "")

    # Create fix task via internal API
    api_url = _get_api_url()
    task_data = {
        "title": f"Fix CI failure (run {run_id})",
        "description": (
            f"CI failed on branch `{head_branch}`.\n\n"
            f"Run URL: {run_url}\n\n"
            "Read the CI logs, identify the failure, and fix the code."
        ),
        "type": "fix",
        "story_id": story_id,
        "project_id": str(project.id),
        "created_by": "system",
        "status": TaskStatus.TODO.value,
    }

    async with httpx.AsyncClient() as client:
        await client.post(f"{api_url}/api/tasks/", json=task_data, timeout=10)

    # Transition story back to in_progress (so dispatcher picks up the fix task)
    await _transition_story_via_api(story_id, "start")

    logger.info(
        "webhook_ci_fix_task_created",
        story_id=story_id,
        run_url=run_url,
    )

    return {"status": "ci_fix_created", "story_id": story_id, "run_url": run_url}
