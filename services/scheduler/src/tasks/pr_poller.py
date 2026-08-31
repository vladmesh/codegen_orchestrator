"""Poll GitHub for merged PRs and CI failures on stories in pr_review status."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING
import uuid

import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.task import TaskStatus
from shared.contracts.dto.users_grant import (
    GrantIntentLifecycleDisposition,
    GrantIntentLifecycleResult,
)
from shared.contracts.queues.deploy import DeployMessage, DeployTrigger
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE
from shared.redis_client import RedisStreamClient

from .. import startup
from ._recipients import resolve_project_recipient
from .story_completion import _parse_owner_repo

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

_COMPLETED_STATUSES = {StoryStatus.COMPLETED.value}
_CI_INFRASTRUCTURE_STEPS = {"Set up Docker Buildx with retry"}


def _ci_failure_limit() -> int:
    return startup.get_config().get_int("scheduler.ci_failure_max_fingerprint_attempts")


def _ci_failure_log_excerpt_lines() -> int:
    return startup.get_config().get_int("scheduler.ci_failure_log_excerpt_lines")


def _failure_fingerprint(failed_jobs: list[dict], unavailable_reason: str | None) -> str:
    payload = [
        {"name": job.get("name"), "failed_steps": job.get("failed_steps", [])}
        for job in failed_jobs
    ] or [{"details_unavailable_reason": unavailable_reason}]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).lower()
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def _needs_initial_owner_seed(
    api_client: SchedulerAPIClient, project_id: str, action: str
) -> bool:
    """Whether this story needs the API-owned initial-owner lifecycle."""
    if action != "create":
        return False
    project = await api_client.get_project(project_id)
    config = getattr(project, "config", None)
    if not isinstance(config, dict) or "tg_bot" not in config.get("modules", []):
        return False
    return True


def _ci_metadata(task: object) -> dict | None:
    metadata = getattr(task, "failure_metadata", None) or {}
    value = metadata.get("ci_failure")
    return value if isinstance(value, dict) else None


def _build_failure_description(evidence: dict) -> str:
    lines = [
        f"CI failed on branch `{evidence['branch']}`.",
        "",
        f"Run URL: {evidence['run_url']}",
        f"Run ID: {evidence['run_id']}",
        f"Head SHA: {evidence['head_sha']}",
        f"Failure fingerprint: {evidence['fingerprint']}",
        f"Fingerprint attempt: {evidence['fingerprint_attempt']}",
        "",
    ]
    if evidence["failed_jobs"]:
        for job in evidence["failed_jobs"]:
            lines.append(f"Job: {job['name']}")
            lines.extend(f"Failed step: {step}" for step in job["failed_steps"])
            if job.get("log_excerpt"):
                lines.extend(["Log excerpt:", "```text", job["log_excerpt"], "```"])
            elif job.get("log_unavailable_reason"):
                lines.append("Job log unavailable: " + job["log_unavailable_reason"])
    else:
        lines.append("Failure details unavailable: " + evidence["details_unavailable_reason"])
    if any(
        step in _CI_INFRASTRUCTURE_STEPS
        for job in evidence["failed_jobs"]
        for step in job["failed_steps"]
    ):
        lines.extend(
            [
                "",
                "CI infrastructure failure: Docker image registry was unavailable while "
                "preparing Buildx after retries.",
                "Do not change application code. Re-run CI when the registry is available.",
            ]
        )
        return "\n".join(lines)
    lines.extend(["", "Fix all reported failures, run local checks, then push once."])
    return "\n".join(lines)


async def _handle_failed_run(
    api_client: SchedulerAPIClient,
    github: GitHubAppClient,
    *,
    owner: str,
    repo_name: str,
    story_id: str,
    project_id: str,
    branch: str,
    run: dict,
) -> bool:
    """Persist one run's evidence and either create a fix or escalate."""
    run_url = run.get("html_url", "")
    run_id = run.get("id", "")
    head_sha = run.get("head_sha") or "unknown"
    tasks = await api_client.get_tasks_by_story(story_id)
    prior_evidence = [item for task in tasks if (item := _ci_metadata(task))]
    if any(item.get("run_id") == run_id for item in prior_evidence):
        return False

    try:
        details = await github.get_workflow_failure_details(
            owner,
            repo_name,
            int(run_id),
            log_excerpt_lines=_ci_failure_log_excerpt_lines(),
        )
    except Exception as exc:
        details = {"failed_jobs": [], "unavailable_reason": type(exc).__name__}
    failed_jobs = details["failed_jobs"]
    unavailable_reason = details.get("unavailable_reason")
    if not failed_jobs and not unavailable_reason:
        unavailable_reason = "GitHub returned no failed jobs"
    fingerprint = _failure_fingerprint(failed_jobs, unavailable_reason)
    same_failure = [item for item in prior_evidence if item.get("fingerprint") == fingerprint]
    attempt = len(same_failure) + 1
    evidence = {
        "run_id": run_id,
        "run_url": run_url,
        "head_sha": head_sha,
        "branch": branch,
        "failed_jobs": failed_jobs,
        "details_unavailable_reason": unavailable_reason,
        "fingerprint": fingerprint,
        "fingerprint_attempt": attempt,
    }

    if attempt > _ci_failure_limit():
        await api_client.transition_story(story_id, "human-review")
        await notify_admins_best_effort(
            f"CI failure {fingerprint} exhausted {_ci_failure_limit()} fix attempts "
            f"for story {story_id}",
            level="warning",
            story_id=story_id,
            failure_fingerprint=fingerprint,
        )
        return False

    task_data = {
        "title": f"Fix CI failure (run {run_id})",
        "description": _build_failure_description(evidence),
        "type": "fix",
        "story_id": story_id,
        "project_id": project_id,
        "created_by": "system",
        "status": TaskStatus.TODO.value,
        "failure_metadata": {"ci_failure": evidence},
    }
    await api_client.create_task(task_data)
    await api_client.transition_story(story_id, "fail")
    await api_client.transition_story(story_id, "reopen")
    await api_client.transition_story(story_id, "start")
    return True


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

        # complete_stories stores the exact PR number — use it for precise lookup.
        # This prevents picking up stale merged PRs from previous QA fix cycles.
        if not story.pr_number:
            log.warning("poll_merged_no_pr_number")
            continue

        try:
            pr_data = await github.get_pull_request(owner, repo_name, story.pr_number)
        except Exception:
            log.exception("poll_merged_github_error", pr_number=story.pr_number)
            continue

        if not pr_data.get("merged_at"):
            continue

        merged_pr = pr_data
        head_sha = merged_pr.get("head", {}).get("sha", "")
        log.info(
            "poll_merged_pr_found",
            pr_number=merged_pr["number"],
            merged_at=merged_pr["merged_at"],
        )

        # Transition story to deploying
        await api_client.transition_story(story_id, "deploy")

        recipient = await resolve_project_recipient(
            api_client, str(project_id), event="deploy_after_pr_merge", story_id=story_id
        )

        # Determine action: "create" for first deploy, "feature" for subsequent
        all_stories = await api_client.get_stories_by_project(project_id)
        has_completed = any(s.status in _COMPLETED_STATUSES for s in all_stories)
        action = "feature" if has_completed else "create"

        # Initial access is an intent lifecycle, never a stable deploy Run.
        # Every merged PR has its own immutable attempt even before a story has
        # completed, which prevents QA/fix cycles from reusing an old SHA.
        if await _needs_initial_owner_seed(api_client, project_id, action):
            lifecycle = GrantIntentLifecycleResult.model_validate(
                await api_client.resume_initial_owner_grant(
                    project_id, story_id=story_id, head_sha=head_sha
                )
            )
            log.info(
                "poll_merged_initial_owner_lifecycle",
                intent_id=lifecycle.intent_id,
                disposition=lifecycle.disposition.value,
                run_id=lifecycle.execution_run_id,
            )
            if lifecycle.disposition is GrantIntentLifecycleDisposition.DISPATCHED:
                deployed += 1
                continue
            if lifecycle.disposition is GrantIntentLifecycleDisposition.EXHAUSTED:
                await api_client.fail_story(story_id)
                await notify_admins_best_effort(
                    f"Grant intent deployment retries exhausted for story {story_id}",
                    level="error",
                    story_id=story_id,
                )
                continue
            if lifecycle.disposition is GrantIntentLifecycleDisposition.IN_FLIGHT:
                log.info(
                    "poll_merged_initial_owner_intent_in_flight", intent_id=lifecycle.intent_id
                )
                continue

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
            telegram_chat_id=recipient.telegram_chat_id,
            unaddressed_reason=recipient.unaddressed_reason,
            story_id=story_id,
            triggered_by=DeployTrigger.WEBHOOK,
            action=action,
            head_sha=head_sha,
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

        try:
            created = await _handle_failed_run(
                api_client,
                github,
                owner=owner,
                repo_name=repo_name,
                story_id=story_id,
                project_id=project_id,
                branch=branch,
                run=run,
            )
        except Exception:
            log.exception("poll_ci_handle_failure_error", run_id=run_id)
            continue
        if created:
            log.info("poll_ci_fix_task_created", run_url=run_url)
            fixed += 1

    return fixed
