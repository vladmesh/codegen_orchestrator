"""Poll GitHub for merged PRs and CI failures on stories in pr_review status."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
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
from shared.contracts.queues.deploy import DeployMessage, DeployOutcome, DeployTrigger
from shared.contracts.worker_evidence import secret_env_values
from shared.diagnostics import redact_diagnostic
from shared.notifications import notify_admins_best_effort
from shared.queues import DEPLOY_QUEUE
from shared.redis import RedisStreamClient

from .. import startup
from ._recipients import resolve_project_recipient
from .image_publication import (
    DEFAULT_BRANCH,
    IMAGE_PUBLICATION_TIMEOUT_SECONDS,
    ImagePublication,
    PublicationVerdict,
    _redacted_failed_jobs,
    image_publication_for_commit,
)
from .story_completion import _parse_owner_repo

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)

_COMPLETED_STATUSES = {StoryStatus.COMPLETED.value}
_CI_INFRASTRUCTURE_STEPS = {"Set up Docker Buildx with retry"}


def _parse_github_timestamp(value: object) -> datetime | None:
    """A GitHub `...Z` timestamp as an aware datetime, or None when unusable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


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


async def _images_ready_for_deploy(
    api_client: SchedulerAPIClient,
    github: GitHubAppClient,
    *,
    owner: str,
    repo_name: str,
    story_id: str,
    head_sha: str,
    deployed_commit_sha: str,
    pull_request: dict,
    existing_timeline: object,
    log: structlog.stdlib.BoundLogger,
) -> bool:
    """Whether this merged commit may be deployed yet, refusing it when it never can.

    True only once the images are observed published. Still building means False
    with the story left exactly where it was, so the next tick asks again and no
    deploy Run has been created to sit there spending a budget on somebody
    else's CI. Never coming means the story is refused, typed and durably, here.
    """
    verdict = await image_publication_for_commit(
        github,
        owner,
        repo_name,
        deployed_commit_sha,
        waiting_since=_parse_github_timestamp(pull_request.get("merged_at")),
        failure_log_excerpt_lines=_ci_failure_log_excerpt_lines(),
        diagnostic_secrets=tuple(secret_env_values(dict(os.environ))),
    )
    timeline = _updated_generated_product_timeline(
        existing_timeline,
        pull_request,
        verdict,
        branch=DEFAULT_BRANCH,
        head_sha=deployed_commit_sha,
    )
    if verdict.state is ImagePublication.PUBLISHED:
        await api_client.update_story(story_id, {"generated_product_timeline": timeline})
        log.info(
            "poll_merged_images_published",
            deployed_commit_sha=deployed_commit_sha,
            ci_run_id=verdict.ci_run_id,
        )
        return True
    if verdict.state is ImagePublication.PENDING:
        await api_client.update_story(story_id, {"generated_product_timeline": timeline})
        log.info(
            "poll_merged_awaiting_image_publication",
            deployed_commit_sha=deployed_commit_sha,
            detail=verdict.detail,
            timeout_seconds=IMAGE_PUBLICATION_TIMEOUT_SECONDS,
        )
        return False
    await _refuse_unpublished_images(
        api_client,
        story_id=story_id,
        head_sha=head_sha,
        deployed_commit_sha=deployed_commit_sha,
        verdict=verdict,
        generated_product_timeline=timeline,
        log=log,
    )
    return False


async def _refuse_unpublished_images(
    api_client: SchedulerAPIClient,
    *,
    story_id: str,
    head_sha: str,
    deployed_commit_sha: str,
    verdict: PublicationVerdict,
    generated_product_timeline: dict,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """End a story whose images never appeared in a state that names that.

    No deploy Run exists yet and none is created: nothing was dispatched, so
    there is nothing for a Run to be the record of. The typed reason therefore
    goes on the story, the same way an infrastructure refusal does, and the story
    joins the human-review queue rather than being failed — a project whose CI
    did not publish is not evidence that the project is broken, and it is not a
    condition another wait can resolve.
    """
    reason = {
        "deploy_outcome": DeployOutcome.IMAGES_NOT_PUBLISHED.value,
        "head_sha": head_sha,
        "deployed_commit_sha": deployed_commit_sha,
        **verdict.evidence(),
    }
    log.error("poll_merged_images_not_published", **reason)
    await api_client.update_story(
        story_id,
        {
            "quarantine_reason": reason,
            "generated_product_timeline": generated_product_timeline,
        },
    )
    await api_client.transition_story(story_id, "human-review")
    await notify_admins_best_effort(
        f"Story {story_id} was not deployed: {verdict.detail}",
        level="error",
        story_id=story_id,
    )


def _updated_generated_product_timeline(
    existing: object,
    pull_request: dict,
    verdict: PublicationVerdict,
    *,
    branch: str | None = None,
    head_sha: str | None = None,
) -> dict:
    """Merge this App-authenticated PR/CI observation into the story record."""
    prior_runs = existing.get("ci_runs") if isinstance(existing, dict) else None
    runs = (
        [dict(item) for item in prior_runs if isinstance(item, dict)]
        if isinstance(prior_runs, list)
        else []
    )
    current_run = None
    if verdict.ci_run_id is not None:
        observed_run = {
            "id": verdict.ci_run_id,
            "url": verdict.ci_run_url,
            "status": verdict.ci_status,
            "conclusion": verdict.ci_conclusion,
            "branch": branch,
            "head_sha": head_sha,
            "failed_jobs": list(verdict.failed_jobs),
            "details_unavailable_reason": verdict.details_unavailable_reason,
        }
        previous = next((item for item in runs if item.get("id") == verdict.ci_run_id), {})
        observed_jobs = observed_run["failed_jobs"]
        previous_jobs = previous.get("failed_jobs")
        if previous_jobs and not observed_jobs:
            observed_run["failed_jobs"] = previous_jobs
        observed_run = {
            key: previous.get(key) if value is None else value
            for key, value in observed_run.items()
        }
        if observed_run["failed_jobs"]:
            observed_run["details_unavailable_reason"] = None
        runs = [item for item in runs if item.get("id") != verdict.ci_run_id]
        runs.append(observed_run)
        current_run = observed_run
    previous_pr = existing.get("pull_request", {}) if isinstance(existing, dict) else {}
    observed_pr = {
        "number": pull_request.get("number"),
        "state": pull_request.get("state"),
        "merged_at": pull_request.get("merged_at"),
        "head_sha": pull_request.get("head", {}).get("sha"),
        "merge_commit_sha": pull_request.get("merge_commit_sha"),
    }
    pr_observation = {
        key: previous_pr.get(key) if value is None else value for key, value in observed_pr.items()
    }
    missed = [
        f"pull request {field} was unavailable"
        for field, value in pr_observation.items()
        if value is None
    ]
    if verdict.ci_run_id is None:
        missed.append(f"CI run identity was unavailable: {verdict.detail}")
    else:
        missed.extend(
            f"CI run {verdict.ci_run_id} {field} was unavailable"
            for field, value in {
                "URL": current_run["url"],
                "status": current_run["status"],
                "branch": current_run["branch"],
                "head SHA": current_run["head_sha"],
            }.items()
            if value is None
        )
        if current_run["conclusion"] == "failure":
            failed_jobs = current_run["failed_jobs"]
            unavailable_reason = current_run["details_unavailable_reason"]
            if not failed_jobs:
                detail = f": {unavailable_reason}" if unavailable_reason else ""
                missed.append(
                    f"CI run {verdict.ci_run_id} failure details were unavailable{detail}"
                )
            for index, job in enumerate(failed_jobs, start=1):
                job_name = job.get("name") or f"#{index}"
                if not job.get("name"):
                    missed.append(f"CI run {verdict.ci_run_id} job {index} name was unavailable")
                if not job.get("failed_steps"):
                    missed.append(
                        f"CI run {verdict.ci_run_id} job {job_name} failed steps were unavailable"
                    )
                if not job.get("log_excerpt"):
                    reason = job.get("log_unavailable_reason")
                    detail = f": {reason}" if reason else ""
                    missed.append(
                        f"CI run {verdict.ci_run_id} job {job_name} log was unavailable{detail}"
                    )
    latest_ci_observation = verdict.evidence()
    if current_run is not None:
        latest_ci_observation.update(
            {
                "ci_run_id": current_run["id"],
                "ci_status": current_run["status"],
                "ci_conclusion": current_run["conclusion"],
                "ci_run_url": current_run["url"],
                "failed_jobs": current_run["failed_jobs"],
                "details_unavailable_reason": current_run["details_unavailable_reason"],
            }
        )
    return {
        "pull_request": pr_observation,
        "ci_runs": runs,
        "latest_ci_observation": latest_ci_observation,
        "missed_captures": list(dict.fromkeys(missed)),
    }


def _has_usable_failed_job_evidence(run: object) -> bool:
    """Whether a stored run can safely skip another failure-detail read."""
    if not isinstance(run, dict):
        return False
    failed_jobs = run.get("failed_jobs")
    return bool(failed_jobs) and all(
        isinstance(job, dict)
        and job.get("name")
        and job.get("failed_steps")
        and (job.get("log_excerpt") or job.get("log_unavailable_reason"))
        for job in failed_jobs
    )


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
    pull_request: dict,
    existing_timeline: object,
) -> bool:
    """Persist one run's evidence and either create a fix or escalate."""
    run_url = run.get("html_url", "")
    run_id = run.get("id", "")
    head_sha = run.get("head_sha") or "unknown"
    tasks = await api_client.get_tasks_by_story(story_id)
    prior_evidence = [item for task in tasks if (item := _ci_metadata(task))]

    timeline_runs = existing_timeline.get("ci_runs") if isinstance(existing_timeline, dict) else []
    timeline_run = (
        next(
            (item for item in timeline_runs if isinstance(item, dict) and item.get("id") == run_id),
            None,
        )
        if isinstance(timeline_runs, list)
        else None
    )
    task_has_run = any(item.get("run_id") == run_id for item in prior_evidence)
    if task_has_run and _has_usable_failed_job_evidence(timeline_run):
        return False

    diagnostic_secrets = tuple(secret_env_values(dict(os.environ)))
    try:
        details = await github.get_workflow_failure_details(
            owner,
            repo_name,
            int(run_id),
            log_excerpt_lines=_ci_failure_log_excerpt_lines(),
        )
    except Exception as exc:
        details = {"failed_jobs": [], "unavailable_reason": type(exc).__name__}
    failed_jobs = list(_redacted_failed_jobs(details["failed_jobs"], secrets=diagnostic_secrets))
    unavailable_reason = details.get("unavailable_reason")
    if unavailable_reason is not None:
        unavailable_reason = redact_diagnostic(
            unavailable_reason,
            secrets=diagnostic_secrets,
        )
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
    verdict = PublicationVerdict(
        state=ImagePublication.REFUSED,
        detail=f"ci.yml run {run_id} on {branch} ended failure",
        ci_run_id=run_id,
        ci_status=run.get("status"),
        ci_conclusion=run.get("conclusion"),
        ci_run_url=run_url or None,
        failed_jobs=tuple(failed_jobs),
        details_unavailable_reason=unavailable_reason,
    )
    timeline = _updated_generated_product_timeline(
        existing_timeline,
        pull_request,
        verdict,
        branch=branch,
        head_sha=head_sha if head_sha != "unknown" else None,
    )
    await api_client.update_story(story_id, {"generated_product_timeline": timeline})
    if task_has_run:
        return False

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
    # One server-side move: the API walks failed -> reopened -> in_progress in a
    # single transaction on the locked row, so a crash cannot park the story in
    # an intermediate status the way three separate calls could.
    await api_client.retry_story_after_ci_failure(story_id)
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
        # What the story produced and what gets deployed are two different
        # commits. No merge method makes the branch's new HEAD equal the pull
        # request head — a merge creates a commit, squash and rebase rewrite
        # one — and the project's CI publishes images from the branch, so the
        # deployed commit is the merge commit and nothing else.
        deployed_commit_sha = merged_pr.get("merge_commit_sha") or ""
        log.info(
            "poll_merged_pr_found",
            pr_number=merged_pr["number"],
            merged_at=merged_pr["merged_at"],
            deployed_commit_sha=deployed_commit_sha,
        )
        if not deployed_commit_sha:
            # Fail closed rather than deploying the pull request head: its
            # images are never published, so the deploy would pull nothing or,
            # worse, something else.
            log.error("poll_merged_no_merge_commit_sha", pr_number=merged_pr["number"])
            continue

        # Nothing is created until this commit's images exist. The story stays in
        # PR_REVIEW while the project's CI is still building, so the next tick
        # asks again; the bound is measured from the merge, so it cannot be
        # restarted by asking.
        if not await _images_ready_for_deploy(
            api_client,
            github,
            owner=owner,
            repo_name=repo_name,
            story_id=story_id,
            head_sha=head_sha,
            deployed_commit_sha=deployed_commit_sha,
            pull_request=merged_pr,
            existing_timeline=getattr(story, "generated_product_timeline", None),
            log=log,
        ):
            continue

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
        seed_lifecycle = None
        if await _needs_initial_owner_seed(api_client, project_id, action):
            seed_lifecycle = GrantIntentLifecycleResult.model_validate(
                await api_client.resume_initial_owner_grant(
                    project_id,
                    story_id=story_id,
                    head_sha=head_sha,
                    deployed_commit_sha=deployed_commit_sha,
                )
            )
            log.info(
                "poll_merged_initial_owner_lifecycle",
                intent_id=seed_lifecycle.intent_id,
                disposition=seed_lifecycle.disposition.value,
                run_id=seed_lifecycle.execution_run_id,
            )
            if seed_lifecycle.disposition is GrantIntentLifecycleDisposition.EXHAUSTED:
                # Failed straight out of PR_REVIEW. Moving the story to DEPLOYING
                # first and then failing it here was two Story transitions on one
                # code path, and the intermediate DEPLOYING had no owner.
                await api_client.fail_story(story_id)
                await notify_admins_best_effort(
                    f"Grant intent deployment retries exhausted for story {story_id}",
                    level="error",
                    story_id=story_id,
                )
                continue

        # The story leaves PR_REVIEW exactly once, on the paths that are actually
        # taking it further.
        await api_client.transition_story(story_id, "deploy")

        if seed_lifecycle is not None:
            if seed_lifecycle.disposition is GrantIntentLifecycleDisposition.DISPATCHED:
                deployed += 1
                continue
            if seed_lifecycle.disposition is GrantIntentLifecycleDisposition.IN_FLIGHT:
                log.info(
                    "poll_merged_initial_owner_intent_in_flight",
                    intent_id=seed_lifecycle.intent_id,
                )
                continue
            if seed_lifecycle.disposition is GrantIntentLifecycleDisposition.STALE_TARGET:
                log.info(
                    "poll_merged_initial_owner_intent_stale_target",
                    intent_id=seed_lifecycle.intent_id,
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
                "deployed_commit_sha": deployed_commit_sha,
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
            deployed_commit_sha=deployed_commit_sha,
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

        pull_request = {"number": getattr(story, "pr_number", None)}
        if isinstance(pull_request["number"], int):
            try:
                pull_request = await github.get_pull_request(
                    owner, repo_name, pull_request["number"]
                )
            except Exception:
                log.exception(
                    "poll_ci_pull_request_error",
                    run_id=run_id,
                    pr_number=pull_request["number"],
                )

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
                pull_request=pull_request,
                existing_timeline=getattr(story, "generated_product_timeline", None),
            )
        except Exception:
            log.exception("poll_ci_handle_failure_error", run_id=run_id)
            continue
        if created:
            log.info("poll_ci_fix_task_created", run_url=run_url)
            fixed += 1

    return fixed
