"""Story completion — PR creation, worker cleanup, and next-story triggering."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.worker import DeleteWorkerCommand
from shared.queues import ARCHITECT_QUEUE, STORY_WORKERS_KEY, WORKER_COMMANDS
from shared.redis_client import RedisStreamClient

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)


def _parse_owner_repo(git_url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub git_url.

    Handles both HTTPS and token-based URLs:
    - https://github.com/org/repo
    - https://x-access-token:TOKEN@github.com/org/repo.git
    """
    # Strip .git suffix and trailing slashes
    url = git_url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    # Take last two path segments
    parts = url.split("/")
    return parts[-2], parts[-1]


async def _cleanup_story_worker(
    redis_client: RedisStreamClient,
    story_id: str,
) -> None:
    """Clean up the worker container associated with a story.

    Reads worker_id from Redis registry, sends DeleteWorkerCommand,
    then clears the registry entry.
    """
    redis = redis_client.redis
    worker_id = await redis.hget(STORY_WORKERS_KEY, story_id)
    if not worker_id:
        return

    if isinstance(worker_id, bytes):
        worker_id = worker_id.decode()

    # Send delete command to worker-manager
    delete_cmd = DeleteWorkerCommand(
        request_id=f"cleanup-story-{story_id}",
        worker_id=worker_id,
        reason="completed",
    )
    await redis_client.publish(WORKER_COMMANDS, delete_cmd.model_dump(mode="json"))

    # Clear registry entry
    await redis.hdel(STORY_WORKERS_KEY, story_id)

    logger.info("story_worker_cleaned_up", story_id=story_id, worker_id=worker_id)


async def _trigger_next_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    project_id: str,
) -> None:
    """Find the next created story for a project and publish to architect:queue."""
    created_stories = await api_client.get_stories_by_status(StoryStatus.CREATED)
    # Filter to same project, sort by priority (lower = higher priority)
    project_stories = sorted(
        [s for s in created_stories if s.get("project_id") == project_id],
        key=lambda s: s.get("priority", 0),
    )
    if not project_stories:
        return

    next_story = project_stories[0]
    arch_msg = ArchitectMessage(
        story_id=next_story["id"],
        project_id=project_id,
        user_id=next_story.get("user_id", ""),
    )
    await redis_client.publish_message(ARCHITECT_QUEUE, arch_msg)
    logger.info(
        "next_story_triggered",
        story_id=next_story["id"],
        project_id=project_id,
    )


async def complete_stories(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Find stories where all tasks are done, create PR for CI gate.

    When all tasks in a story are done:
    1. Create PR from story/{story_id} → main
    2. Enable auto-merge (merge commit, not squash — preserves individual commits)
    3. Transition story to PR_REVIEW
    4. Cleanup worker container, trigger next story

    Deploy is triggered later by the webhook handler when PR is merged to main.

    Returns the number of stories transitioned.
    """
    stories = await api_client.get_stories_by_status(StoryStatus.IN_PROGRESS)
    completed = 0

    if stories:
        logger.info(
            "complete_stories_check",
            in_progress_stories=len(stories),
        )

    for story in stories:
        story_id = story["id"]
        project_id = story.get("project_id")

        tasks = await api_client.get_tasks_by_story(story_id)

        # Skip if no tasks (architect may not have run yet)
        if not tasks:
            logger.debug("complete_stories_skip_no_tasks", story_id=story_id)
            continue

        task_statuses = [t.get("status") for t in tasks]
        # Check if all tasks are done
        if not all(s == TaskStatus.DONE for s in task_statuses):
            logger.debug(
                "complete_stories_skip_not_all_done",
                story_id=story_id,
                task_statuses=task_statuses,
            )
            continue

        log = logger.bind(story_id=story_id, project_id=project_id)

        # Get repository to create PR
        repo = await api_client.get_primary_repository(project_id) if project_id else None
        if not repo:
            log.error("complete_stories_no_repo", project_id=project_id)
            continue

        git_url = repo.get("git_url", "")
        owner, repo_name = _parse_owner_repo(git_url)
        story_title = story.get("title", f"Story {story_id}")
        branch = f"story/{story_id}"

        # Create PR from story branch to main
        try:
            github = GitHubAppClient()
            pr = await github.create_pull_request(
                owner,
                repo_name,
                head=branch,
                base="main",
                title=story_title,
                body="All tasks completed. Auto-merge enabled.",
            )
            pr_number = pr["number"]
            pr_node_id = pr.get("node_id", "")
            log.info(
                "story_pr_created",
                pr_number=pr_number,
                branch=branch,
                node_id=pr_node_id[:20] if pr_node_id else "",
            )

            # Enable auto-merge (merge commit to preserve individual commits)
            # node_id must be a GraphQL ID (e.g. "PR_kwDO..."), not a number
            if pr_node_id and isinstance(pr_node_id, str) and not pr_node_id.isdigit():
                auto_merged = await github.enable_auto_merge(
                    owner, repo_name, pr_node_id=pr_node_id
                )
            else:
                log.warning(
                    "story_pr_node_id_invalid",
                    pr_number=pr_number,
                    node_id_raw=repr(pr_node_id),
                )
                # Fetch node_id via REST as fallback
                pr_details = await github.get_pull_request(owner, repo_name, pr_number)
                pr_node_id = pr_details.get("node_id", "")
                if pr_node_id and not pr_node_id.isdigit():
                    auto_merged = await github.enable_auto_merge(
                        owner, repo_name, pr_node_id=pr_node_id
                    )
                else:
                    log.error("story_pr_node_id_fetch_failed", pr_number=pr_number)
                    auto_merged = False
            if not auto_merged:
                log.warning("story_auto_merge_failed", pr_number=pr_number)
        except Exception:
            log.exception("story_pr_creation_failed", branch=branch)
            continue

        # Transition story to pr_review (webhook handles deploy after merge)
        await api_client.transition_story(story_id, "pr_review")
        log.info("story_pr_review", task_count=len(tasks), pr_number=pr_number)

        # Cleanup story worker container (no longer needed)
        await _cleanup_story_worker(redis_client, story_id)

        # Trigger next queued story for this project (doesn't need PR to merge)
        await _trigger_next_story(api_client, redis_client, project_id)

        completed += 1

    return completed
