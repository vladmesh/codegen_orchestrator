"""Unit tests for `complete_stories` PR-creation failure classification.

A pull request GitHub refuses with 422 "No commits between ..." is not a
transient error: the story branch carries no commit of its own, so every later
tick asks the same impossible question. Such a story leaves the retry set with
its reason recorded; every other PR-creation error keeps retrying.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from shared.clients.github import NoCommitsBetweenError
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.story import WAITING_ON_BY_STATUS, StoryDTO, StoryStatus
from shared.contracts.dto.task import TaskDTO
from src.tasks.story_completion import STORY_NO_COMMITS_REASON, complete_stories

_NOW = datetime.now(UTC)
_PROJ_ID = "00000000-0000-0000-0000-000000000001"
_STORY_HEAD_SHA = "a" * 40


def _story(story_id: str = "story-1", *, pr_number: int | None = None) -> StoryDTO:
    return StoryDTO(
        id=story_id,
        project_id=UUID(_PROJ_ID),
        title=story_id,
        type="product",
        status=StoryStatus.IN_PROGRESS,
        waiting_on=WAITING_ON_BY_STATUS[StoryStatus.IN_PROGRESS],
        priority=0,
        created_by="system",
        pr_number=pr_number,
        created_at=_NOW,
    )


def _done_task() -> TaskDTO:
    return TaskDTO(
        id="task-A",
        project_id=UUID(_PROJ_ID),
        type="feature",
        title="task-A",
        description="",
        status="done",
        priority=0,
        current_iteration=0,
        max_iterations=3,
        created_by="system",
        story_id="story-1",
        blocked_by_task_id=None,
        dispatch_admitted=True,
        created_at=_NOW,
    )


def _repo() -> RepositoryDTO:
    return RepositoryDTO(
        id="repo-1",
        project_id=UUID(_PROJ_ID),
        name="test-project",
        git_url="https://github.com/org/test-project",
        role="primary",
        visibility="private",
        is_managed=True,
        created_at=_NOW,
    )


@pytest.fixture
def api_client():
    client = AsyncMock()
    client.get_stories_by_status.return_value = [_story()]
    client.get_tasks_by_story.return_value = [_done_task()]
    client.list_runs.return_value = []
    client.get_primary_repository.return_value = _repo()
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.redis = AsyncMock()
    client.redis.hget = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_no_commits_between_takes_the_story_out_of_the_retry_set(api_client, redis_client):
    """A 422 no-commits refusal parks the story instead of asking again next tick."""
    github = AsyncMock()
    github.get_ref_sha.return_value = _STORY_HEAD_SHA
    github.create_pull_request.side_effect = NoCommitsBetweenError(
        "Cannot open PR story/story-1->main: No commits between main and story/story-1."
    )

    with patch("src.tasks.story_completion.GitHubAppClient", return_value=github):
        completed = await complete_stories(api_client, redis_client)

    assert completed == 0
    api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
    reason = api_client.update_story.await_args.args[1]["quarantine_reason"]
    assert reason["reason"] == STORY_NO_COMMITS_REASON
    assert reason["branch"] == "story/story-1"
    assert "No commits between" in reason["detail"]


@pytest.mark.asyncio
async def test_a_parked_story_is_not_selected_by_the_next_completion_cycle(
    api_client, redis_client
):
    selected = [_story()]
    api_client.get_stories_by_status.side_effect = lambda status: (
        selected if status == StoryStatus.IN_PROGRESS else []
    )

    async def park(story_id, action):
        assert (story_id, action) == ("story-1", "human-review")
        selected.clear()

    api_client.transition_story.side_effect = park
    github = AsyncMock()
    github.get_ref_sha.return_value = _STORY_HEAD_SHA
    github.create_pull_request.side_effect = NoCommitsBetweenError(
        "Cannot open PR story/story-1->main: No commits between main and story/story-1."
    )

    with patch("src.tasks.story_completion.GitHubAppClient", return_value=github):
        assert await complete_stories(api_client, redis_client) == 0
        assert await complete_stories(api_client, redis_client) == 0

    assert github.create_pull_request.await_count == 1
    assert api_client.get_tasks_by_story.await_count == 1


@pytest.mark.asyncio
async def test_generic_pr_creation_error_keeps_the_story_in_progress(api_client, redis_client):
    """A transient GitHub error keeps its current behaviour: retry on the next tick."""
    github = AsyncMock()
    github.get_ref_sha.return_value = _STORY_HEAD_SHA
    github.create_pull_request.side_effect = RuntimeError("GitHub is having a bad day")

    with patch("src.tasks.story_completion.GitHubAppClient", return_value=github):
        completed = await complete_stories(api_client, redis_client)

    assert completed == 0
    api_client.transition_story.assert_not_awaited()
    api_client.update_story.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_commits_rejects_a_stale_merged_pr_with_an_earlier_head():
    from src.tasks.story_completion import _resolve_current_cycle_pr

    current_sha = "b" * 40
    github = AsyncMock()
    github.get_ref_sha.return_value = current_sha
    github.create_pull_request.side_effect = NoCommitsBetweenError("No commits between")
    github.get_pull_request.return_value = {
        "number": 3,
        "merged_at": "2026-09-13T15:00:00Z",
        "head": {"ref": "story/story-1", "sha": "a" * 40},
    }

    with pytest.raises(NoCommitsBetweenError):
        await _resolve_current_cycle_pr(
            github,
            story=_story(pr_number=3),
            owner="org",
            repo_name="repo",
            branch="story/story-1",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pull_request",
    [
        {"node_id": "PR_missing_number", "head": {"sha": "a" * 40}},
        {"number": "3", "head": {"sha": "a" * 40}},
        {"number": 3, "head": {}},
        {"number": 3, "head": {"sha": "b" * 40}},
    ],
)
async def test_created_pr_requires_an_unambiguous_current_head_identity(pull_request):
    from src.tasks.story_completion import _resolve_current_cycle_pr

    github = AsyncMock()
    github.get_ref_sha.return_value = "a" * 40
    github.create_pull_request.return_value = pull_request

    with pytest.raises(ValueError, match="current-cycle pull request"):
        await _resolve_current_cycle_pr(
            github,
            story=_story(),
            owner="org",
            repo_name="repo",
            branch="story/story-1",
        )
