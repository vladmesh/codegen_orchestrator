"""Approve deploying a repaired default-branch head for an `images_not_published` park.

The merged-PR poller refuses a story whose built commit never got images and
parks it for human review; the merge commit's CI run is over, so its images can
never appear. Once the product's CI is repaired on the default branch, an
administrator approves a later commit of that branch. This puts the story back
on the poller path with that approval recorded: the poller, and only the
poller, still decides CREATE or FEATURE, the initial-owner seed and the deploy
Run. Nothing here creates a Run or publishes a message.
"""

from datetime import UTC, datetime
import re
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from shared.clients.github import GitHubAppClient
from shared.contracts.dto.commit_publication import EngineeringStop
from shared.contracts.dto.repaired_head_deploy import (
    REPAIRED_HEAD_APPROVAL_KEY,
    RepairedHeadDeployApproval,
    RepairedHeadDeployCommand,
)
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.deploy import DeployOutcome
from shared.models import Repository, Run
from shared.models.story import Story

from .attempt_disposition import release_engineering_stop
from .catalog_install_recovery import _github_repository

logger = structlog.get_logger()

_LIVE_RUN_STATUSES = (RunStatus.QUEUED.value, RunStatus.RUNNING.value)


def _refuse(code: str, detail: str) -> NoReturn:
    raise HTTPException(409, {"code": code, "detail": detail})


async def deploy_repaired_head(
    story_id: str, command: RepairedHeadDeployCommand, actor: str, db: AsyncSession
) -> Story:
    """Record the approval and return the story to `pr_review`, or refuse with a 409.

    Every refusal comes before any write, so a refused call changes nothing.
    """
    from .routers._story_helpers import _do_transition, _get_story_for_update

    story = await _get_story_for_update(story_id, db)
    cause = story.quarantine_reason
    if story.status != StoryStatus.WAITING_HUMAN_REVIEW.value:
        _refuse("story_not_waiting_human_review", "The story is not parked for human review.")
    if (
        not isinstance(cause, dict)
        or cause.get("deploy_outcome") != DeployOutcome.IMAGES_NOT_PUBLISHED.value
    ):
        _refuse("not_images_not_published", "The park is not an images_not_published refusal.")
    stop = (
        EngineeringStop.model_validate(story.engineering_stop) if story.engineering_stop else None
    )
    if stop is None or stop.released_at is not None or stop.id != command.stop_id:
        _refuse("engineering_stop_mismatch", "stop_id is not the story's unreleased stop.")
    if story.pr_number is None:
        _refuse("story_has_no_pull_request", "The story records no merged pull request.")
    live = await db.scalar(
        select(Run.id)
        .where(
            Run.story_id == story.id,
            Run.type == RunType.DEPLOY.value,
            Run.status.in_(_LIVE_RUN_STATUSES),
        )
        .limit(1)
    )
    if live is not None:
        _refuse("deploy_run_live", f"Deploy Run {live} of the story is queued or running.")
    superseded = cause.get("deployed_commit_sha")
    if not isinstance(superseded, str) or not re.fullmatch(r"[0-9a-f]{40}", superseded):
        _refuse("refused_commit_unknown", "The refusal names no 40-hex deployed commit.")
    if command.deployed_commit_sha == superseded:
        _refuse("commit_not_ahead", "The commit is the one the refusal already named.")

    repository = await db.scalar(
        select(Repository).where(
            Repository.project_id == story.project_id, Repository.role == "primary"
        )
    )
    if repository is None:
        _refuse("repository_missing", "The project has no primary repository.")
    owner, name = _github_repository(repository)
    async with GitHubAppClient() as github:
        pull_request = await github.get_pull_request(owner, name, story.pr_number)
        merge_commit_sha = pull_request.get("merge_commit_sha")
        head_sha = (pull_request.get("head") or {}).get("sha")
        if not pull_request.get("merged_at") or not merge_commit_sha:
            _refuse("pull_request_not_merged", f"PR #{story.pr_number} is not merged.")
        if head_sha != cause.get("head_sha"):
            _refuse(
                "pull_request_changed",
                f"PR #{story.pr_number} head is not the head the refusal named.",
            )
        default_branch = (await github.get_repo(owner, name)).default_branch
        if not await github.branch_contains_commit(
            owner, name, default_branch, command.deployed_commit_sha
        ):
            _refuse(
                "commit_not_on_default_branch",
                f"The commit is not on the default branch {default_branch}.",
            )
        comparison = await github.compare_commits_status(
            owner, name, superseded, command.deployed_commit_sha
        )
        if comparison != "ahead":
            _refuse(
                "commit_not_ahead",
                f"The commit is {comparison}, not ahead of the refused commit {superseded}.",
            )

    approval = RepairedHeadDeployApproval(
        actor=actor,
        approved_at=datetime.now(UTC),
        pr_number=story.pr_number,
        head_sha=head_sha,
        merge_commit_sha=merge_commit_sha,
        approved_commit_sha=command.deployed_commit_sha,
        superseded_commit_sha=superseded,
        quarantine_reason=cause,
    )
    release_engineering_stop(story, command.stop_id, actor, db, expected_cause=cause)
    story.generated_product_timeline = {
        **(story.generated_product_timeline or {}),
        REPAIRED_HEAD_APPROVAL_KEY: approval.model_dump(mode="json"),
    }
    story.quarantine_reason = None
    _do_transition(story, StoryStatus.PR_REVIEW)
    await db.commit()
    logger.info(
        "story_repaired_head_deploy_approved",
        story_id=story.id,
        actor=actor,
        approved_commit_sha=approval.approved_commit_sha,
        superseded_commit_sha=approval.superseded_commit_sha,
    )
    return story
