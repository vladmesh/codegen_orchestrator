"""Unit tests for poll_merged_prs — exact PR lookup via story.pr_number."""

from unittest.mock import AsyncMock, patch

import pytest
from tasks.pr_poller import poll_merged_prs


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
@patch("tasks.pr_poller.GitHubAppClient")
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
        "head": {"sha": "fix-sha"},
    }

    await poll_merged_prs(api, redis)

    # Must call get_pull_request with exact PR number, not list_pull_requests
    gh.get_pull_request.assert_called_once_with("org", "my-repo", 42)
    gh.list_pull_requests.assert_not_called()

    deploy_msg = redis.publish_message.call_args[0][1]
    assert deploy_msg.head_sha == "fix-sha"


@pytest.mark.asyncio
@patch("tasks.pr_poller.GitHubAppClient")
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
@patch("tasks.pr_poller.GitHubAppClient")
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
@patch("tasks.pr_poller.GitHubAppClient")
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
        "head": {"sha": "fix-sha-correct"},
    }

    await poll_merged_prs(api, redis)

    deploy_msg = redis.publish_message.call_args[0][1]
    assert deploy_msg.head_sha == "fix-sha-correct"
    # Never calls list_pull_requests — no chance to pick up stale PR #3
    gh.list_pull_requests.assert_not_called()
