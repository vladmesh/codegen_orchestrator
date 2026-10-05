"""Story completion — PR creation, worker cleanup, and next-story triggering."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from shared.clients.github import GitHubAppClient, NoCommitsBetweenError
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryDTO, StoryStatus
from shared.contracts.dto.story_failure import StoryFailure, StoryFailureCode
from shared.contracts.dto.task import TaskStatus, TaskType
from shared.contracts.queues.architect import ArchitectMessage
from shared.queues import ARCHITECT_QUEUE
from shared.redis import RedisStreamClient

from ._github_refs import _parse_owner_repo
from ._recipients import resolve_project_recipient
from .story_worker_teardown import finalize_story_worker_teardown
from .supervisor.common import STORY_HUMAN_REVIEW_ACTION

if TYPE_CHECKING:
    from ..clients.api import SchedulerAPIClient

logger = structlog.get_logger(__name__)


def _validate_current_cycle_pr(pr: object, *, branch: str, branch_sha: str) -> dict:
    """Require an exact, unambiguous identity for the current branch state."""
    if not isinstance(pr, dict):
        raise ValueError("current-cycle pull request response is not an object")
    number = pr.get("number")
    head = pr.get("head")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise ValueError("current-cycle pull request has no valid number")
    if not isinstance(head, dict):
        raise ValueError("current-cycle pull request has no head identity")
    if head.get("ref") != branch or head.get("sha") != branch_sha:
        raise ValueError("current-cycle pull request does not match the current branch head")
    return pr


async def _resolve_current_cycle_pr(
    github: GitHubAppClient,
    *,
    story: StoryDTO,
    owner: str,
    repo_name: str,
    branch: str,
) -> dict:
    """Resolve the PR representing the exact current story-branch state."""
    branch_sha = await github.get_ref_sha(owner, repo_name, f"heads/{branch}")
    if not isinstance(branch_sha, str) or not branch_sha:
        raise ValueError(f"current story branch {branch} has no commit SHA")

    try:
        pr = await github.create_pull_request(
            owner,
            repo_name,
            head=branch,
            base="main",
            title=story.title,
            body="All tasks completed. The pipeline merges it once checks pass.",
        )
    except NoCommitsBetweenError as no_commits:
        if not story.pr_number:
            raise
        try:
            stored_pr = await github.get_pull_request(owner, repo_name, story.pr_number)
        except Exception as exc:
            raise RuntimeError("could not verify stored PR after no-commits response") from exc
        try:
            _validate_current_cycle_pr(stored_pr, branch=branch, branch_sha=branch_sha)
        except ValueError:
            raise no_commits from None
        if stored_pr["number"] != story.pr_number or not stored_pr.get("merged_at"):
            raise no_commits
        return stored_pr

    return _validate_current_cycle_pr(pr, branch=branch, branch_sha=branch_sha)


async def _trigger_next_story(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    project_id: str,
) -> None:
    """Find the next created story for a project and publish to architect:queue."""
    created_stories = await api_client.get_stories_by_status(StoryStatus.CREATED)
    # Filter to same project, sort by priority (lower = higher priority)
    project_stories = sorted(
        [s for s in created_stories if str(s.project_id) == project_id],
        key=lambda s: s.priority,
    )
    if not project_stories:
        return

    next_story = project_stories[0]
    recipient = await resolve_project_recipient(
        api_client, project_id, event="next_story_triggered", story_id=next_story.id
    )
    arch_msg = ArchitectMessage(
        story_id=next_story.id,
        project_id=project_id,
        telegram_chat_id=recipient.telegram_chat_id,
    )
    await redis_client.publish_message(ARCHITECT_QUEUE, arch_msg)
    logger.info(
        "next_story_triggered",
        story_id=next_story.id,
        project_id=project_id,
    )


async def _has_live_deploy_fix(api_client: SchedulerAPIClient, story_id: str) -> bool:
    """Whether a taskless deploy-fix engineering attempt still owns the story branch."""
    story_runs = await api_client.list_runs(story_id=story_id, run_type=RunType.ENGINEERING.value)
    return any(
        run.type == RunType.ENGINEERING
        and run.status in (RunStatus.QUEUED, RunStatus.RUNNING)
        and "deploy_fix_attempt" in run.run_metadata
        for run in story_runs
    )


async def _park_story_without_commits(
    api_client: SchedulerAPIClient,
    story_id: str,
    branch: str,
    detail: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Atomically park the story with its cause and owed owner/admin notices."""
    failure = StoryFailure(
        code=StoryFailureCode.NO_NEW_COMMIT,
        source="scheduler",
        detail=f"Branch {branch}: {detail}",
    )
    try:
        await api_client.stop_story(story_id, STORY_HUMAN_REVIEW_ACTION, failure, actor="scheduler")
    except Exception as exc:
        log.error("story_no_commits_stop_failed", branch=branch, error_type=type(exc).__name__)
        return
    log.warning("story_parked_without_commits", branch=branch)


async def _has_recovered_taskless_commit(api_client, story_id):
    recovery = await api_client.get_story_recovered_commit(story_id)
    if recovery is None:
        return False
    if recovery.receipt is None or not recovery.receipt.published:
        raise ValueError("Recovered Story requires exact publication proof")
    return True


async def _install_publication_refusal(tasks, story, pr, github, owner, repo_name, branch):
    installs = [
        task for task in tasks if task.type is TaskType.INSTALL and task.status is TaskStatus.DONE
    ]
    install_refusal = None
    for installed in installs:
        operation = installed.install_operation
        if operation is None or operation.state != "published" or not operation.head_sha:
            install_refusal = "Install Task lacks its exact durable publication head"
            break
        if operation.cycle_started_at != (story.reopened_at or story.created_at):
            install_refusal = "Install Task belongs to an older story cycle"
            break
        if not await github.branch_contains_commit(owner, repo_name, branch, operation.head_sha):
            install_refusal = "Story PR branch does not contain the published install head"
            break
    if (
        install_refusal is None
        and installs
        and all(
            task.type is TaskType.INSTALL or task.status is TaskStatus.CANCELLED for task in tasks
        )
    ):
        latest = max(installs, key=lambda task: task.created_at)
        if pr["head"]["sha"] != latest.install_operation.head_sha:
            install_refusal = "Install Story PR head differs from its exact published head"
    return install_refusal


async def complete_stories(  # noqa: PLR0915  # PR owner also verifies mechanical publication
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
) -> int:
    """Find stories whose live tasks are all done, create PR for CI gate.

    A *live* task is one that is not cancelled. A cancelled task is work that
    was decided against, not work still outstanding, so waiting on it waits for
    ever; a story whose tasks are *all* cancelled is not finished either and is
    left where it is.

    When all live tasks in a story are done:
    1. Read story/{story_id} HEAD and resolve its exact current-cycle PR
    2. Persist that PR number; GitHub auto-merge is never enabled, because the PR
       poller is the only automated merger (see ``pr_poller``)
    3. Finalize worker removal and its unchanged story binding
    4. Transition story to PR_REVIEW, then trigger the next story

    Deploy is triggered later by poll_merged_prs() when PR is merged to main.

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
        try:
            story_id, project_id = story.id, str(story.project_id)

            tasks = await api_client.get_tasks_by_story(story_id)

            # Ordinary no-roster Stories still await their architect. An
            # explicit recovered taskless commit already proves engineering;
            # use this same PR/CI/deploy owner rather than creating a Task or
            # another engineering Run to manufacture discovery.
            if not tasks:
                if not await _has_recovered_taskless_commit(api_client, story_id):
                    logger.debug("complete_stories_skip_no_tasks", story_id=story_id)
                    continue

            task_statuses = [t.status for t in tasks]
            # A cancelled task is not outstanding work, so it cannot be waited on.
            # Counting it was what stranded a story for ever: an operator cancelling
            # a task through `DELETE /api/tasks/{id}`, and — since the Product Brief
            # boundary exists — the corpse of a superseded plan, which the takeover
            # cancels because nothing will ever release it.
            live_statuses = [s for s in task_statuses if s != TaskStatus.CANCELLED]
            if tasks and not live_statuses:
                # Every task cancelled is not a finished story: there is nothing on
                # the branch to open a PR for. Somebody has to decide what happens
                # to this story, so it stays in progress rather than completing.
                logger.debug(
                    "complete_stories_skip_all_cancelled",
                    story_id=story_id,
                    task_count=len(task_statuses),
                )
                continue
            # Check if all non-cancelled tasks are done
            if not all(s == TaskStatus.DONE for s in live_statuses):
                logger.debug(
                    "complete_stories_skip_not_all_done",
                    story_id=story_id,
                    task_statuses=task_statuses,
                )
                continue

            # A deploy code-fix has no Task row, but still writes to the story
            # branch using the registered story worker. Do not publish a PR or
            # tear that worker down while its engineering attempt is live.
            if await _has_live_deploy_fix(api_client, story_id):
                logger.debug("complete_stories_skip_live_deploy_fix", story_id=story_id)
                continue

            log = logger.bind(story_id=story_id, project_id=project_id)

            # Get repository to create PR
            repo = await api_client.get_primary_repository(project_id) if project_id else None
            if not repo:
                log.error("complete_stories_no_repo", project_id=project_id)
                continue

            git_url = repo.git_url or ""
            owner, repo_name = _parse_owner_repo(git_url)
            branch = f"story/{story_id}"

            # Create PR from story branch to main
            try:
                # One GitHub HTTP pool spans every GitHub call of this completion and is
                # closed on success and error alike.
                async with GitHubAppClient() as github:
                    pr = await _resolve_current_cycle_pr(
                        github,
                        story=story,
                        owner=owner,
                        repo_name=repo_name,
                        branch=branch,
                    )
                    install_refusal = await _install_publication_refusal(
                        tasks, story, pr, github, owner, repo_name, branch
                    )
                    if install_refusal is not None:
                        await api_client.stop_story(
                            story_id,
                            "human-review",
                            StoryFailure(
                                code=StoryFailureCode.SCAFFOLD_FAILED,
                                source="scheduler",
                                detail=install_refusal
                                + "; inspect the retained operation and branch.",
                            ),
                            actor="scheduler",
                        )
                        continue
                    pr_number = pr["number"]
                    await api_client.update_story(story_id, {"pr_number": pr_number})
                    pr_node_id = pr.get("node_id", "")
                    pr_merged = pr.get("merged_at") is not None

                    if pr_merged:
                        # PR already merged while the story was in_progress (e.g. a
                        # person merged it, or an auto-merge request enabled before the
                        # PR poller became the only automated merger fired).
                        # Transition to pr_review so poll_merged_prs() picks it up
                        # and triggers deploy.
                        log.info(
                            "story_pr_already_merged",
                            pr_number=pr_number,
                            branch=branch,
                        )
                        if not await finalize_story_worker_teardown(
                            redis_client,
                            story_id=story_id,
                            project_id=project_id,
                            request_id=f"pr-review-story-{story_id}",
                        ):
                            continue
                        await api_client.transition_story(story_id, "pr_review")
                        await _trigger_next_story(api_client, redis_client, project_id)
                        completed += 1
                        continue

                    # No GitHub auto-merge: a merge GitHub performs later, by itself,
                    # starts the product's push-main CI with whatever registry secrets
                    # the repository holds by then. The PR poller re-reads this PR after
                    # checks settle, writes the current registry secrets and merges in
                    # the same tick, or parks a GitHub refusal with notices.
                    log.info(
                        "story_pr_created",
                        pr_number=pr_number,
                        branch=branch,
                        node_id=pr_node_id[:20] if pr_node_id else "",
                    )
            except NoCommitsBetweenError as no_commits:
                # Not a transient error: the branch carries no commit of its own, so
                # every later tick asks GitHub the same impossible question and gets
                # the same 422. Take the story out of the retry set with the reason
                # attached, and leave the decision to a person.
                log.warning("story_pr_no_commits_between", branch=branch)
                await _park_story_without_commits(
                    api_client, story_id, branch, str(no_commits), log
                )
                continue
            except Exception:
                log.exception("story_pr_creation_failed", branch=branch)
                continue

            if not await finalize_story_worker_teardown(
                redis_client,
                story_id=story_id,
                project_id=project_id,
                request_id=f"pr-review-story-{story_id}",
            ):
                continue

            # Transition story to pr_review (poll_merged_prs handles deploy after merge)
            await api_client.transition_story(story_id, "pr_review")
            log.info("story_pr_review", task_count=len(tasks), pr_number=pr_number)

            # Trigger next queued story for this project (doesn't need PR to merge)
            await _trigger_next_story(api_client, redis_client, project_id)

            completed += 1

        except Exception:
            logger.exception("story_completion_contained", story_id=story.id)
            continue

    return completed
