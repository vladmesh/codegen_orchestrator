"""Poll GitHub for merged PRs on stories in pr_review status."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from .story_completion import _parse_owner_repo

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)


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
        story_id = story["id"]
        project_id = story.get("project_id")
        log = logger.bind(story_id=story_id, project_id=project_id)

        if not project_id:
            continue

        repo = await api_client.get_primary_repository(project_id)
        if not repo:
            log.warning("poll_merged_no_repo")
            continue

        git_url = repo.get("git_url", "")
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

        # Resolve user_id for deploy message
        story_data = await api_client.get_story(story_id)
        user_id = story_data.get("user_id", "")

        # Publish deploy message
        run_id = f"deploy-poll-{uuid.uuid4().hex[:8]}"
        run_data = {
            "id": run_id,
            "type": "deploy",
            "project_id": str(project_id),
            "run_metadata": {
                "triggered_by": "pr_poll",
                "head_sha": head_sha,
                "story_id": story_id,
            },
        }
        await api_client.create_run(run_data)

        deploy_msg = DeployMessage(
            task_id=run_id,
            project_id=str(project_id),
            user_id=str(user_id),
            story_id=story_id,
            triggered_by=DeployTrigger.WEBHOOK,
            action="feature",
        )
        await redis_client.publish_message(DEPLOY_QUEUE, deploy_msg)

        log.info("poll_merged_deploy_triggered", run_id=run_id)
        deployed += 1

    return deployed
