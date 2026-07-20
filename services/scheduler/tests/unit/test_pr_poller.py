"""Unit tests for poll_merged_prs — exact PR lookup via story.pr_number."""

from unittest.mock import AsyncMock, patch

import pytest

from src.tasks.pr_poller import _failure_fingerprint, poll_ci_failures, poll_merged_prs


def _make_story(story_id="story-1", project_id="proj-1", pr_number=None):
    """Minimal story-like object."""
    s = AsyncMock()
    s.id = story_id
    s.project_id = project_id
    s.pr_number = pr_number
    return s


def _make_repo(git_url="https://github.com/org/my-repo"):
    r = AsyncMock()
    r.git_url = git_url
    return r


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_uses_pr_number_for_exact_lookup(mock_gh_cls):
    """poll_merged_prs fetches the exact PR by story.pr_number,
    not by scanning all closed PRs on the branch.
    """
    gh = AsyncMock()
    mock_gh_cls.return_value = gh

    api = AsyncMock()
    redis = AsyncMock()

    story = _make_story(pr_number=42)
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = _make_repo()
    api.get_stories_by_project.return_value = []

    gh.get_pull_request.return_value = {
        "number": 42,
        "merged_at": "2026-03-20T03:15:00Z",
        "head": {"sha": "a" * 40},
    }

    await poll_merged_prs(api, redis)

    # Must call get_pull_request with exact PR number, not list_pull_requests
    gh.get_pull_request.assert_called_once_with("org", "my-repo", 42)
    gh.list_pull_requests.assert_not_called()

    deploy_msg = redis.publish_message.call_args[0][1]
    assert deploy_msg.head_sha == "a" * 40


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_skips_story_without_pr_number(mock_gh_cls):
    """Stories without pr_number are skipped (backward compat / edge case)."""
    gh = AsyncMock()
    mock_gh_cls.return_value = gh

    api = AsyncMock()
    redis = AsyncMock()

    story = _make_story(pr_number=None)
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = _make_repo()

    deployed = await poll_merged_prs(api, redis)

    assert deployed == 0
    gh.get_pull_request.assert_not_called()
    redis.publish_message.assert_not_called()


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_skips_unmerged_pr(mock_gh_cls):
    """If the exact PR exists but isn't merged yet, skip."""
    gh = AsyncMock()
    mock_gh_cls.return_value = gh

    api = AsyncMock()
    redis = AsyncMock()

    story = _make_story(pr_number=42)
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = _make_repo()

    gh.get_pull_request.return_value = {
        "number": 42,
        "merged_at": None,
        "head": {"sha": "pending-sha"},
    }

    deployed = await poll_merged_prs(api, redis)

    assert deployed == 0
    redis.publish_message.assert_not_called()


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_deploys_correct_sha_in_fix_cycle(mock_gh_cls):
    """QA fix cycle: story has pr_number=5 (the fix PR).
    Old PR #3 is also merged on same branch — but we only look at #5.
    """
    gh = AsyncMock()
    mock_gh_cls.return_value = gh

    api = AsyncMock()
    redis = AsyncMock()

    # Story points to the fix PR, not the original
    story = _make_story(pr_number=5)
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = _make_repo()
    api.get_stories_by_project.return_value = []

    # PR #5 is merged with the fix SHA
    gh.get_pull_request.return_value = {
        "number": 5,
        "merged_at": "2026-03-20T03:30:00Z",
        "head": {"sha": "b" * 40},
    }

    await poll_merged_prs(api, redis)

    deploy_msg = redis.publish_message.call_args[0][1]
    assert deploy_msg.head_sha == "b" * 40
    # Never calls list_pull_requests — no chance to pick up stale PR #3
    gh.list_pull_requests.assert_not_called()


def _failed_run(run_id: int, sha: str) -> dict:
    return {
        "id": run_id,
        "status": "completed",
        "conclusion": "failure",
        "html_url": f"https://github.com/org/my-repo/actions/runs/{run_id}",
        "head_sha": sha,
    }


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.notify_admins_best_effort", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_ci_failure_evidence_is_actionable_and_idempotent(mock_gh_cls, notify):
    gh = AsyncMock()
    mock_gh_cls.return_value = gh
    api = AsyncMock()
    story = _make_story()
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = _make_repo()
    api.get_tasks_by_story.return_value = []
    gh.get_latest_workflow_run.return_value = _failed_run(101, "sha-101")
    gh.get_workflow_failure_details.return_value = {
        "failed_jobs": [{"name": "unit", "failed_steps": ["Run pytest"]}],
        "unavailable_reason": None,
    }
    api.create_task.return_value.id = "fix-101"

    assert await poll_ci_failures(api) == 1
    task = api.create_task.call_args.args[0]
    evidence = task["failure_metadata"]["ci_failure"]
    assert evidence == {
        "run_id": 101,
        "run_url": "https://github.com/org/my-repo/actions/runs/101",
        "head_sha": "sha-101",
        "branch": "story/story-1",
        "failed_jobs": [{"name": "unit", "failed_steps": ["Run pytest"]}],
        "details_unavailable_reason": None,
        "fingerprint": evidence["fingerprint"],
        "fingerprint_attempt": 1,
    }
    assert "Job: unit" in task["description"]
    assert "Failed step: Run pytest" in task["description"]
    assert "sha-101" in task["description"]
    notify.assert_not_awaited()

    prior = AsyncMock()
    prior.failure_metadata = task["failure_metadata"]
    api.get_tasks_by_story.return_value = [prior]
    api.create_task.reset_mock()
    assert await poll_ci_failures(api) == 0
    api.create_task.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.notify_admins_best_effort", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_three_same_fingerprints_create_two_fixes_then_escalate(mock_gh_cls, notify):
    gh = AsyncMock()
    mock_gh_cls.return_value = gh
    api = AsyncMock()
    story = _make_story()
    api.get_stories_by_status.return_value = [story]
    api.get_primary_repository.return_value = _make_repo()
    details = {
        "failed_jobs": [{"name": "lint", "failed_steps": ["Ruff"]}],
        "unavailable_reason": None,
    }
    gh.get_workflow_failure_details.return_value = details
    prior = []

    for run_id in (201, 202):
        api.get_tasks_by_story.return_value = prior
        gh.get_latest_workflow_run.return_value = _failed_run(run_id, f"sha-{run_id}")
        api.create_task.return_value.id = f"fix-{run_id}"
        assert await poll_ci_failures(api) == 1
        created = api.create_task.call_args.args[0]
        task = AsyncMock()
        task.failure_metadata = created["failure_metadata"]
        prior.append(task)

    api.get_tasks_by_story.return_value = prior
    gh.get_latest_workflow_run.return_value = _failed_run(203, "sha-203")
    api.create_task.reset_mock()
    assert await poll_ci_failures(api) == 0
    api.create_task.assert_not_awaited()
    api.transition_story.assert_awaited_with("story-1", "human-review")
    notify.assert_awaited_once()


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.notify_admins_best_effort", new_callable=AsyncMock)
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_exhausted_failure_retries_story_transition(mock_gh_cls, notify):
    gh = AsyncMock()
    mock_gh_cls.return_value = gh
    api = AsyncMock()
    api.get_stories_by_status.return_value = [_make_story()]
    api.get_primary_repository.return_value = _make_repo()
    details = {
        "failed_jobs": [{"name": "lint", "failed_steps": ["Ruff"]}],
        "unavailable_reason": None,
    }
    gh.get_workflow_failure_details.return_value = details
    gh.get_latest_workflow_run.return_value = _failed_run(203, "sha-203")

    prior = []
    for run_id in (201, 202):
        task = AsyncMock()
        task.failure_metadata = {
            "ci_failure": {
                "run_id": run_id,
                "fingerprint": "placeholder",
            }
        }
        prior.append(task)
    fingerprint = _failure_fingerprint(details["failed_jobs"], None)
    for task in prior:
        task.failure_metadata["ci_failure"]["fingerprint"] = fingerprint
    api.get_tasks_by_story.return_value = prior
    api.transition_story.side_effect = [RuntimeError("temporary"), None]

    assert await poll_ci_failures(api) == 0
    notify.assert_not_awaited()
    assert await poll_ci_failures(api) == 0

    assert api.transition_story.await_count == 2
    notify.assert_awaited_once()
    api.create_task.assert_not_awaited()
    api.update_task.assert_not_awaited()


@pytest.mark.asyncio
@patch("src.tasks.pr_poller.GitHubAppClient")
async def test_ci_failure_records_details_unavailability(mock_gh_cls):
    gh = AsyncMock()
    mock_gh_cls.return_value = gh
    api = AsyncMock()
    api.get_stories_by_status.return_value = [_make_story()]
    api.get_primary_repository.return_value = _make_repo()
    api.get_tasks_by_story.return_value = []
    gh.get_latest_workflow_run.return_value = _failed_run(301, "sha-301")
    gh.get_workflow_failure_details.side_effect = RuntimeError("token leaked if copied")

    assert await poll_ci_failures(api) == 1
    task = api.create_task.call_args.args[0]
    assert task["failure_metadata"]["ci_failure"]["details_unavailable_reason"] == "RuntimeError"
    assert "token leaked" not in task["description"]
