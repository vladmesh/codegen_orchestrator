"""The merged-PR poller deploys an administrator-approved repaired head.

A story refused for `images_not_published` comes back to `pr_review` carrying an
approval of a later default-branch commit. The poller deploys that commit
through its ordinary path; every other story behaves exactly as before.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _github_client_context import self_entering
from _owner_notification_claims import ClaimsFromWrites
import pytest

from shared.contracts.dto.repaired_head_deploy import REPAIRED_HEAD_APPROVAL_KEY
from shared.contracts.queues.deploy import DeployMessage
from shared.queues import DEPLOY_QUEUE
from src.tasks.deploy_dispatch import deploy_run_id
from src.tasks.pr_poller import poll_merged_prs

PR_HEAD = "a" * 40
MERGE = "e" * 40
REPAIRED = "c" * 40
#: Long past: the merge's own 15-minute bound has expired.
MERGED_AT = "2026-10-01T09:00:00Z"


def _approval(approved_at: datetime, *, pr_number: int = 5, approved: str = REPAIRED) -> dict:
    return {
        "actor": "user:7",
        "approved_at": approved_at.isoformat(),
        "pr_number": pr_number,
        "head_sha": PR_HEAD,
        "merge_commit_sha": MERGE,
        "approved_commit_sha": approved,
        "superseded_commit_sha": MERGE,
        "quarantine_reason": {"deploy_outcome": "images_not_published"},
    }


def _story(approval: dict | None) -> SimpleNamespace:
    timeline = {"pull_request": {"number": 5, "merge_commit_sha": MERGE}}
    if approval is not None:
        timeline[REPAIRED_HEAD_APPROVAL_KEY] = approval
    return SimpleNamespace(
        id="story-repaired",
        project_id="proj-1",
        pr_number=5,
        generated_product_timeline=timeline,
    )


def _ci_run(sha: str, *, status: str = "completed", conclusion: str | None = "success") -> dict:
    return {
        "id": 9200,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://github.com/fictional-org/recipe-box/actions/runs/9200",
        "created_at": "2026-10-08T10:00:00Z",
        "head_sha": sha,
    }


def _world(gh: AsyncMock, story: SimpleNamespace, run: dict) -> AsyncMock:
    api = AsyncMock()
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = SimpleNamespace(
        git_url="https://github.com/fictional-org/recipe-box"
    )
    api.get_stories_by_project.return_value = []
    gh.get_pull_request.return_value = {
        "number": 5,
        "state": "closed",
        "merged_at": MERGED_AT,
        "merge_commit_sha": MERGE,
        "head": {"sha": PR_HEAD},
    }
    gh.get_latest_workflow_run.return_value = run
    return api


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_an_approved_story_deploys_the_repaired_head_through_the_ordinary_path(mock_gh_cls):
    gh = AsyncMock()
    mock_gh_cls.return_value = self_entering(gh)
    approved_at = datetime.now(UTC) - timedelta(minutes=2)
    api = _world(gh, _story(_approval(approved_at)), _ci_run(REPAIRED))
    redis = AsyncMock()

    assert await poll_merged_prs(api, redis) == 1

    # The images asked about are the approved commit's.
    assert gh.get_latest_workflow_run.await_args.kwargs["head_sha"] == REPAIRED
    run = api.create_run_if_absent.await_args.args[0]
    expected_id = deploy_run_id(
        "deploy-approved", "story-repaired", REPAIRED, approved_at.isoformat()
    )
    assert run["id"] == expected_id
    assert run["id"] not in {
        deploy_run_id("deploy-poll", "story-repaired", MERGE),
        deploy_run_id("deploy-poll", "story-repaired", REPAIRED),
    }
    assert run["story_id"] == "story-repaired"
    metadata = run["run_metadata"]
    assert metadata["triggered_by"] == "pr_poll"
    assert metadata["head_sha"] == PR_HEAD
    assert metadata["deployed_commit_sha"] == REPAIRED
    assert metadata["merge_commit_sha"] == MERGE
    assert metadata[REPAIRED_HEAD_APPROVAL_KEY]["actor"] == "user:7"
    api.transition_story.assert_awaited_once_with("story-repaired", "deploy")
    # The deploy message carries the story, so the deploy handler's settings
    # seed, platform key, QA and completion follow it as for any story deploy.
    queue, message = redis.publish_message.await_args.args
    assert queue == DEPLOY_QUEUE and isinstance(message, DeployMessage)
    assert message.story_id == "story-repaired"
    assert message.task_id == expected_id
    assert message.head_sha == PR_HEAD
    assert message.deployed_commit_sha == REPAIRED
    assert message.action.value == "create"
    # The approval stays on the story record the poller rewrote.
    timeline = api.update_story.await_args_list[0].args[1]["generated_product_timeline"]
    assert timeline[REPAIRED_HEAD_APPROVAL_KEY]["approved_commit_sha"] == REPAIRED
    assert timeline["deploy_observation"]["story_id"] == "story-repaired"


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_the_image_bound_is_measured_from_the_approval_not_the_merge(mock_gh_cls):
    """The merge was a week ago; the repaired head's CI is still building."""
    gh = AsyncMock()
    mock_gh_cls.return_value = self_entering(gh)
    approval = _approval(datetime.now(UTC) - timedelta(minutes=1))
    api = _world(gh, _story(approval), _ci_run(REPAIRED, status="in_progress", conclusion=None))
    redis = AsyncMock()

    assert await poll_merged_prs(api, redis) == 0

    api.transition_story.assert_not_awaited()
    api.create_run_if_absent.assert_not_awaited()
    update = api.update_story.await_args.args[1]
    assert "quarantine_reason" not in update
    assert update["generated_product_timeline"][REPAIRED_HEAD_APPROVAL_KEY] == approval


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.notify_admins_best_effort", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_an_approval_older_than_the_bound_refuses_at_the_approved_commit(mock_gh_cls, notify):
    gh = AsyncMock()
    mock_gh_cls.return_value = self_entering(gh)
    approval = _approval(datetime.now(UTC) - timedelta(minutes=20))
    api = _world(gh, _story(approval), _ci_run(REPAIRED, status="queued", conclusion=None))
    ClaimsFromWrites(api)
    redis = AsyncMock()

    assert await poll_merged_prs(api, redis) == 0

    reason = api.update_story.await_args_list[0].args[1]["quarantine_reason"]
    assert reason["deploy_outcome"] == "images_not_published"
    assert reason["deployed_commit_sha"] == REPAIRED
    api.transition_story.assert_awaited_once_with("story-repaired", "human-review")
    api.create_run_if_absent.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.notify_admins_best_effort", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_an_approved_head_whose_images_fail_is_refused_again_naming_it(mock_gh_cls, notify):
    """The new refusal names the approved commit, so the next approval must descend from it."""
    gh = AsyncMock()
    mock_gh_cls.return_value = self_entering(gh)
    approval = _approval(datetime.now(UTC) - timedelta(minutes=3))
    api = _world(gh, _story(approval), _ci_run(REPAIRED, conclusion="failure"))
    gh.get_workflow_failure_details.return_value = {
        "failed_jobs": [
            {
                "name": "build",
                "failed_steps": ["Build image"],
                "log_excerpt": "BindingEnvironmentError",
                "log_unavailable_reason": None,
            }
        ],
        "unavailable_reason": None,
    }
    ClaimsFromWrites(api)
    redis = AsyncMock()

    assert await poll_merged_prs(api, redis) == 0

    update = api.update_story.await_args_list[0].args[1]
    reason = update["quarantine_reason"]
    assert reason["deploy_outcome"] == "images_not_published"
    assert reason["head_sha"] == PR_HEAD
    assert reason["deployed_commit_sha"] == REPAIRED
    assert reason["ci_run_id"] == 9200
    assert update["generated_product_timeline"][REPAIRED_HEAD_APPROVAL_KEY] == approval
    api.transition_story.assert_awaited_once_with("story-repaired", "human-review")
    api.create_run_if_absent.assert_not_awaited()
    redis.publish_message.assert_not_awaited()
    notify.assert_awaited_once()


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.notify_admins_best_effort", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_an_approval_of_another_pull_request_is_history_not_an_instruction(
    mock_gh_cls, notify
):
    """A story's later PR deploys its own merge commit, measured from its own merge."""
    gh = AsyncMock()
    mock_gh_cls.return_value = self_entering(gh)
    approval = _approval(datetime.now(UTC), pr_number=4)
    api = _world(gh, _story(approval), _ci_run(MERGE, status="in_progress", conclusion=None))
    ClaimsFromWrites(api)
    redis = AsyncMock()

    assert await poll_merged_prs(api, redis) == 0

    assert gh.get_latest_workflow_run.await_args.kwargs["head_sha"] == MERGE
    # The merge's own bound expired long ago, so the pending run is refused.
    reason = api.update_story.await_args_list[0].args[1]["quarantine_reason"]
    assert reason["deployed_commit_sha"] == MERGE


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_the_initial_owner_seed_is_asked_for_the_approved_commit(mock_gh_cls):
    gh = AsyncMock()
    mock_gh_cls.return_value = self_entering(gh)
    api = _world(gh, _story(_approval(datetime.now(UTC))), _ci_run(REPAIRED))
    api.get_project.return_value = SimpleNamespace(
        config={"modules": ["backend", "tg_bot"]}, owner_id=7
    )
    api.resume_initial_owner_grant.return_value = {
        "intent_id": "users-grant-initial_owner-recipe-box-84",
        "disposition": "dispatched",
        "status": "queued",
        "execution_run_id": "deploy-grant-attempt",
        "target": {"application_id": None, "deployment_id": None, "sha": PR_HEAD},
    }
    redis = AsyncMock()

    assert await poll_merged_prs(api, redis) == 1

    api.resume_initial_owner_grant.assert_awaited_once_with(
        "proj-1",
        story_id="story-repaired",
        head_sha=PR_HEAD,
        deployed_commit_sha=REPAIRED,
        merged_pr_number=5,
    )
    api.transition_story.assert_awaited_once_with("story-repaired", "deploy")
