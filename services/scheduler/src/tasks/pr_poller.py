"""Poll GitHub for merged PRs and CI failures on stories in pr_review status."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from .story_completion import _parse_owner_repo

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

_COMPLETED_STATUSES = {StoryStatus.COMPLETED.value, "completed"}


async def poll_merged_prs(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Poll GitHub for merged PRs on stories in pr_review status.

    Replaces the webhook dependency: if a story branch PR was merged to main,
    transition story to deploying and publish deploy message.

    Returns the number of stories transitioned to deploying.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.PR_REVIEW)
    if not stories:
        return 0

    deployed = 0
    github = GitHubAppClient()

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        if not project_id:
            continue

        repo = await api_client.get_primary_repository(project_id)
        if not repo:
            log.warning("poll_merged_no_repo")
            continue

        git_url = repo.git_url or ""
        owner, repo_name = _parse_owner_repo(git_url)
        branch = f"story/{story_id}"

        try:
            prs = await github.list_pull_requests(
                owner, repo_name, head=branch, base="main", state="closed"
            )
        except Exception:
            log.exception("poll_merged_github_error")
            continue

        # Find a merged PR for this story branch
        merged_pr = next((pr for pr in prs if pr.get("merged_at")), None)
        if not merged_pr:
            continue

        head_sha = merged_pr.get("head", {}).get("sha", "")
        log.info(
            "poll_merged_pr_found",
            pr_number=merged_pr["number"],
            merged_at=merged_pr["merged_at"],
        )

        # Transition story to deploying
        await api_client.transition_story(story_id, "deploy")

        # StoryDTO has no user_id field
        user_id = ""

        # Determine action: "create" for first deploy, "feature" for subsequent
        all_stories = await api_client.get_stories_by_project(project_id)
        has_completed = any(s.status in _COMPLETED_STATUSES for s in all_stories)
        action = "feature" if has_completed else "create"

        # Publish deploy message
        run_id = f"deploy-poll-{uuid.uuid4().hex[:8]}"
        run_data = {
            "id": run_id,
            "type": "deploy",
            "project_id": str(project_id),
            "story_id": story_id,
            "run_metadata": {
                "triggered_by": "pr_poll",
                "head_sha": head_sha,
            },
        }
        await api_client.create_run(run_data)

        deploy_msg = DeployMessage(
            task_id=run_id,
            project_id=str(project_id),
            user_id=str(user_id),
            story_id=story_id,
            triggered_by=DeployTrigger.WEBHOOK,
            action=action,
        )
        await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)

        log.info("poll_merged_deploy_triggered", run_id=run_id)
        deployed += 1

    return deployed


async def poll_ci_failures(
    api_client: SchedulerAPIClient,
) -> int:
    """Check CI status on open PRs for stories in pr_review.

    If CI failed on the story branch, create a fix task and transition
    the story back to in_progress so the dispatcher picks it up.

    Returns the number of fix tasks created.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.PR_REVIEW)
    if not stories:
        return 0

    fixed = 0
    github = GitHubAppClient()

    for story in stories:
        story_id = story.id
        project_id = str(story.project_id)
        log = logger.bind(story_id=story_id, project_id=project_id)

        repo = await api_client.get_primary_repository(project_id)
        if not repo:
            continue

        git_url = repo.git_url or ""
        owner, repo_name = _parse_owner_repo(git_url)
        branch = f"story/{story_id}"

        try:
            run = await github.get_latest_workflow_run(
                owner,
                repo_name,
                workflow_file="ci.yml",
                branch=branch,
            )
        except Exception:
            log.exception("poll_ci_github_error")
            continue

        if not run:
            continue

        if run.get("status") != "completed":
            continue

        if run.get("conclusion") != "failure":
            continue

        run_url = run.get("html_url", "")
        run_id = run.get("id", "")
        log.info("poll_ci_failure_detected", run_url=run_url, run_id=run_id)

        # Create fix task
        task_data = {
            "title": f"Fix CI failure (run {run_id})",
            "description": (
                f"CI failed on branch `{branch}`.\n\n"
                f"Run URL: {run_url}\n\n"
                "Read the CI logs, identify the failure, and fix the code."
            ),
            "type": "fix",
            "story_id": story_id,
            "project_id": project_id,
            "created_by": "system",
            "status": TaskStatus.TODO.value,
        }

        try:
            await api_client.create_task(task_data)
        except Exception:
            log.exception("poll_ci_create_task_error")
            continue

        # Transition story: pr_review → failed → reopened → in_progress
        try:
            await api_client.transition_story(story_id, "fail")
            await api_client.transition_story(story_id, "reopen")
            await api_client.transition_story(story_id, "start")
        except Exception:
            log.exception("poll_ci_story_transition_error")
            continue

        log.info("poll_ci_fix_task_created", run_url=run_url)
        fixed += 1

    return fixed
