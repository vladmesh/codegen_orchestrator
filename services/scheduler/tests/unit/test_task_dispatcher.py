"""Tests for task dispatcher — dispatches todo tasks and completes stories."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO, TaskEventDTO

# ---------------------------------------------------------------------------
# Helper factories — build valid DTO instances with sensible defaults
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _task(**overrides) -> TaskDTO:
    defaults = {
        "id": "task-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "type": "feature",
        "title": "Default task",
        "description": None,
        "plan": None,
        "status": "todo",
        "priority": 0,
        "acceptance_criteria": None,
        "current_iteration": 0,
        "max_iterations": 3,
        "need_e2e": False,
        "created_by": "system",
        "source_brainstorm_id": None,
        "repository_id": None,
        "story_id": None,
        "blocked_by_task_id": None,
        "failure_metadata": None,
        "last_event": None,
        "elapsed_minutes": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return TaskDTO.model_validate(defaults)


def _task_event(**overrides) -> TaskEventDTO:
    defaults = {
        "id": 1,
        "task_id": "task-1",
        "event_type": "iteration_end",
        "from_status": None,
        "to_status": None,
        "iteration": None,
        "details": {},
        "actor": "system",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return TaskEventDTO.model_validate(defaults)


def _story(**overrides) -> StoryDTO:
    defaults = {
        "id": "story-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "parent_story_id": None,
        "title": "Default story",
        "description": None,
        "acceptance_criteria": None,
        "type": "product",
        "status": "in_progress",
        "priority": 0,
        "blocked_by_story_id": None,
        "created_by": "system",
        "user_report": None,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return StoryDTO.model_validate(defaults)


def _repo(**overrides) -> RepositoryDTO:
    defaults = {
        "id": "repo-1",
        "project_id": "00000000-0000-0000-0000-000000000001",
        "name": "weather-bot",
        "git_url": "https://github.com/my-org/weather-bot",
        "provider_repo_id": None,
        "role": "primary",
        "visibility": "private",
        "is_managed": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    defaults.update(overrides)
    return RepositoryDTO.model_validate(defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJ_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def api_client():
    from unittest.mock import MagicMock

    from shared.contracts.dto.project import ProjectStatus

    client = AsyncMock()
    # Default project mock — active (scaffolded) project with workspace ready
    project_mock = MagicMock()
    project_mock.id = "proj-1"
    project_mock.status = ProjectStatus.ACTIVE.value
    project_mock.config = {"workspace_ready": True}
    client.get_project.return_value = project_mock
    # Default: project has existing applications (feature deploy)
    client.get_applications_by_project.return_value = [{"id": 1, "status": "running"}]
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish_flat = AsyncMock()
    client.redis = AsyncMock()
    client.redis.hget = AsyncMock(return_value=None)
    client.redis.hdel = AsyncMock()
    client.redis.xadd = AsyncMock()
    return client


class TestDispatchTodoTasks:
    """Dispatch unblocked todo tasks to engineering queue."""

    @pytest.mark.asyncio
    async def test_dispatches_unblocked_task(self, api_client, redis_client):
        """Task with no blocker gets a run created and published."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create User SQLAlchemy model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_task_events.return_value = []
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # Should create a run
        api_client.create_run.assert_called_once()
        run_data = api_client.create_run.call_args[0][0]
        assert run_data["type"] == "engineering"
        assert run_data["project_id"] == PROJ_ID

        # Should publish to engineering queue
        redis_client.publish_message.assert_called_once()

        # Should transition task to in_dev
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_skips_blocked_task(self, api_client, redis_client):
        """Task blocked by non-done task is skipped."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add API endpoint",
                description="REST endpoint",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id="task-1",
                status="todo",
            )
        ]
        api_client.get_task.return_value = _task(id="task-1", status="in_dev")

        await dispatch_todo_tasks(api_client, redis_client)

        # Should NOT create a run
        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_task_when_project_is_draft(self, api_client, redis_client):
        """Task is skipped if project is still in draft (not yet scaffolded)."""
        from unittest.mock import MagicMock

        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create User SQLAlchemy model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        from shared.contracts.dto.project import ProjectStatus

        project_mock = MagicMock()
        project_mock.status = ProjectStatus.DRAFT.value
        api_client.get_project.return_value = project_mock

        await dispatch_todo_tasks(api_client, redis_client)

        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_task_when_workspace_not_ready(self, api_client, redis_client):
        """Task is skipped if project workspace is not ready."""
        from unittest.mock import MagicMock

        from shared.contracts.dto.project import ProjectStatus
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add feature",
                description="A feature",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]

        project_mock = MagicMock()
        project_mock.status = ProjectStatus.ACTIVE.value
        project_mock.config = {}  # workspace_ready not set
        api_client.get_project.return_value = project_mock

        await dispatch_todo_tasks(api_client, redis_client)

        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_task_when_blocker_done(self, api_client, redis_client):
        """Task whose blocker is done gets dispatched."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add API endpoint",
                description="REST endpoint",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id="task-1",
                status="todo",
            )
        ]
        api_client.get_task.return_value = _task(id="task-1", status="done")
        api_client.get_task_events.return_value = [
            _task_event(
                event_type="iteration_end",
                details={"commit_sha": "abc", "summary": "Done"},
            )
        ]
        api_client.create_run.return_value = {"id": "run-2"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        api_client.create_run.assert_called_once()
        redis_client.publish_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_includes_cumulative_context(self, api_client, redis_client):
        """Dispatched task includes context from completed sibling tasks."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add API endpoint",
                description="REST endpoint",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id="task-1",
                status="todo",
            )
        ]
        api_client.get_task.return_value = _task(id="task-1", status="done")
        # Sibling tasks for story-1: task-1 (done) and task-2 (todo)
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="todo", story_id="story-1", project_id=PROJ_ID),
        ]
        # Events for task-1 (the done sibling)
        api_client.get_task_events.return_value = [
            _task_event(
                event_type="iteration_end",
                details={
                    "commit_sha": "abc123",
                    "summary": "Created User model with email field",
                },
            )
        ]
        api_client.create_run.return_value = {"id": "run-2"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # The engineering message should have enriched description
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert "User model" in eng_msg.description
        assert eng_msg.planning_task_id == "task-2"

    @pytest.mark.asyncio
    async def test_includes_story_id_in_engineering_message(self, api_client, redis_client):
        """Dispatched task includes story_id for worker reuse."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_task_events.return_value = []
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.story_id == "story-1"

    @pytest.mark.asyncio
    async def test_story_id_none_for_standalone_task(self, api_client, redis_client):
        """Task without story_id -> story_id=None in message."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Standalone task",
                description="No story",
                type="feature",
                project_id=PROJ_ID,
                story_id=None,
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}

        await dispatch_todo_tasks(api_client, redis_client)

        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.story_id is None

    @pytest.mark.asyncio
    async def test_skips_task_when_sibling_rejected(self, api_client, redis_client):
        """Todo task in story with a worker-rejected sibling -> not dispatched."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add endpoint",
                description="REST API",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        # Sibling task-1 failed with worker_rejected metadata
        api_client.get_tasks_by_story.return_value = [
            _task(
                id="task-1",
                status="failed",
                failure_metadata={"failure_reason": "worker_rejected"},
                story_id="story-1",
                project_id=PROJ_ID,
            ),
            _task(id="task-2", status="todo", story_id="story-1", project_id=PROJ_ID),
        ]

        await dispatch_todo_tasks(api_client, redis_client)

        # Should NOT dispatch — story has a rejected task
        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_when_sibling_failed_normally(self, api_client, redis_client):
        """Todo task with a normally-failed sibling (no reject) -> still dispatched."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add endpoint",
                description="REST API",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        # Sibling task-1 failed normally (no reject metadata)
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="failed", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="todo", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_task_events.return_value = []
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # Should dispatch — normal failure doesn't block siblings
        api_client.create_run.assert_called_once()
        redis_client.publish_message.assert_called_once()


class TestBranchInDispatch:
    """Tests that branch is included in EngineeringMessage."""

    @pytest.mark.asyncio
    async def test_dispatch_includes_branch_for_story_task(self, api_client, redis_client):
        """Task with story_id gets branch=story/{story_id} in EngineeringMessage."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Add user model",
                description="Create User SQLAlchemy model",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-abc",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_task_events.return_value = []
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-abc", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        redis_client.publish_message.assert_called_once()
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.branch == "story/story-abc"

    @pytest.mark.asyncio
    async def test_dispatch_no_branch_for_standalone_task(self, api_client, redis_client):
        """Task without story_id gets branch=None."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                title="Fix bug",
                description="Fix it",
                type="fix",
                project_id=PROJ_ID,
                story_id=None,
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.create_run.return_value = {"id": "run-1"}
        api_client.transition_task.return_value = {}

        await dispatch_todo_tasks(api_client, redis_client)

        redis_client.publish_message.assert_called_once()
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.branch is None


class TestParseOwnerRepo:
    """Parse owner/repo from GitHub git URLs."""

    def test_https_url(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        assert _parse_owner_repo("https://github.com/my-org/my-repo") == ("my-org", "my-repo")

    def test_https_url_with_git_suffix(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        assert _parse_owner_repo("https://github.com/my-org/my-repo.git") == ("my-org", "my-repo")

    def test_token_url(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        url = "https://x-access-token:ghs_abc@github.com/my-org/my-repo.git"
        assert _parse_owner_repo(url) == ("my-org", "my-repo")

    def test_trailing_slash(self):
        from src.tasks.task_dispatcher import _parse_owner_repo

        assert _parse_owner_repo("https://github.com/org/repo/") == ("org", "repo")


class TestCompleteStories:
    """Complete stories when all tasks are done."""

    @pytest.mark.asyncio
    async def test_completes_story_creates_pr_when_all_tasks_done(self, api_client, redis_client):
        """Story with all tasks done -> creates PR, enables auto-merge, transitions to pr_review."""
        from unittest.mock import patch

        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            name="weather-bot",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_abc",
            "html_url": "https://github.com/my-org/weather-bot/pull/42",
        }
        mock_github.enable_auto_merge.return_value = True

        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        # Should transition story to pr_review (not deploying)
        api_client.transition_story.assert_called_once_with("story-1", "pr_review")

        # Should create PR from story branch to main
        mock_github.create_pull_request.assert_called_once_with(
            "my-org",
            "weather-bot",
            head="story/story-1",
            base="main",
            title="Add weather API",
            body="All tasks completed. Auto-merge enabled.",
        )

        # Should enable auto-merge
        mock_github.enable_auto_merge.assert_called_once_with(
            "my-org", "weather-bot", pr_node_id="PR_abc"
        )

        # Should NOT publish deploy message (webhook handles it after merge)
        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if "deploy" in str(c).lower()
        ]
        assert len(deploy_calls) == 0

    @pytest.mark.asyncio
    async def test_no_complete_when_tasks_pending(self, api_client, redis_client):
        """Story with pending tasks -> no action."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [_story(id="story-1", project_id=PROJ_ID)]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
            _task(id="task-2", status="in_dev", story_id="story-1", project_id=PROJ_ID),
        ]

        await complete_stories(api_client, redis_client)

        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_complete_when_no_tasks(self, api_client, redis_client):
        """Story with zero tasks -> no action (architect may not have run yet)."""
        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [_story(id="story-1", project_id=PROJ_ID)]
        api_client.get_tasks_by_story.return_value = []

        await complete_stories(api_client, redis_client)

        api_client.transition_story.assert_not_called()


class TestSuperviseFailedTasks:
    """Supervisor skips worker-rejected tasks."""

    @pytest.mark.asyncio
    async def test_skips_worker_rejected_task(self, api_client, redis_client):
        """Failed task with worker_rejected metadata -> not retried."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                story_id="story-1",
                current_iteration=0,
                max_iterations=3,
                failure_metadata={"failure_reason": "worker_rejected"},
                status="failed",
                project_id=PROJ_ID,
            )
        ]

        result = await supervise_failed_tasks(api_client, redis_client)

        # Should NOT retry — worker rejected, needs admin
        api_client.transition_task.assert_not_called()
        assert result["retried"] == 0
        assert result["failed"] == 0

    @pytest.mark.asyncio
    async def test_skips_developer_blocked_task(self, api_client, redis_client):
        """Failed task with developer_blocked metadata -> not retried."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                story_id="story-1",
                current_iteration=0,
                max_iterations=3,
                failure_metadata={"failure_reason": "developer_blocked"},
                status="failed",
                project_id=PROJ_ID,
            )
        ]

        result = await supervise_failed_tasks(api_client, redis_client)

        api_client.transition_task.assert_not_called()
        assert result["retried"] == 0
        assert result["failed"] == 0


class TestDispatchSkipsDeveloperBlocked:
    """Dispatcher skips stories with developer-blocked siblings."""

    @pytest.mark.asyncio
    async def test_skips_task_when_sibling_developer_blocked(self, api_client, redis_client):
        """Todo task in story with a developer_blocked sibling -> not dispatched."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-2",
                title="Add endpoint",
                description="REST API",
                type="feature",
                project_id=PROJ_ID,
                story_id="story-1",
                blocked_by_task_id=None,
                status="todo",
            )
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(
                id="task-1",
                status="waiting_human_review",
                failure_metadata={"failure_reason": "developer_blocked"},
                story_id="story-1",
                project_id=PROJ_ID,
            ),
            _task(id="task-2", status="todo", story_id="story-1", project_id=PROJ_ID),
        ]

        await dispatch_todo_tasks(api_client, redis_client)

        api_client.create_run.assert_not_called()
        redis_client.publish_message.assert_not_called()


class TestPollMergedPRs:
    """Poll GitHub for merged PRs on stories in pr_review."""

    @pytest.mark.asyncio
    async def test_triggers_deploy_when_pr_merged(self, api_client, redis_client):
        """Story in pr_review with merged PR -> deploy triggered."""
        from unittest.mock import patch

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review")
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)
        api_client.transition_story.return_value = {}
        api_client.create_run.return_value = {}

        mock_github = AsyncMock()
        mock_github.list_pull_requests.return_value = [
            {
                "number": 42,
                "merged_at": "2026-03-16T12:00:00Z",
                "head": {"sha": "abc123"},
            }
        ]

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=mock_github):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        api_client.transition_story.assert_called_once_with("story-1", "deploy")
        api_client.create_run.assert_called_once()
        redis_client.publish_message.assert_called_once()

        # Verify deploy message contents
        call_args = redis_client.publish_message.call_args
        assert call_args[0][0] == "deploy:queue"
        deploy_msg = call_args[0][1]
        assert deploy_msg.project_id == PROJ_ID
        assert deploy_msg.story_id == "story-1"
        assert deploy_msg.action == "feature"

    @pytest.mark.asyncio
    async def test_no_action_when_pr_not_merged(self, api_client, redis_client):
        """Story in pr_review with closed but not merged PR -> no action."""
        from unittest.mock import patch

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review")
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )

        mock_github = AsyncMock()
        mock_github.list_pull_requests.return_value = [
            {"number": 42, "merged_at": None, "head": {"sha": "abc123"}}
        ]

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=mock_github):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 0
        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_action_when_no_stories_in_pr_review(self, api_client, redis_client):
        """No stories in pr_review -> nothing to poll."""
        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = []

        result = await poll_merged_prs(api_client, redis_client)

        assert result == 0

    @pytest.mark.asyncio
    async def test_continues_on_github_error(self, api_client, redis_client):
        """GitHub API error for one story doesn't block others."""
        from unittest.mock import patch

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review"),
            _story(
                id="story-2",
                project_id="00000000-0000-0000-0000-000000000002",
                status="pr_review",
            ),
        ]
        api_client.get_primary_repository.side_effect = [
            _repo(
                id="repo-1",
                git_url="https://github.com/my-org/repo1",
                project_id=PROJ_ID,
            ),
            _repo(
                id="repo-2",
                git_url="https://github.com/my-org/repo2",
                project_id="00000000-0000-0000-0000-000000000002",
            ),
        ]
        api_client.get_story.return_value = _story(
            id="story-2",
            project_id="00000000-0000-0000-0000-000000000002",
        )
        api_client.transition_story.return_value = {}
        api_client.create_run.return_value = {}

        mock_github = AsyncMock()
        mock_github.list_pull_requests.side_effect = [
            Exception("GitHub API error"),
            [{"number": 10, "merged_at": "2026-03-16T12:00:00Z", "head": {"sha": "def456"}}],
        ]

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=mock_github):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        api_client.transition_story.assert_called_once_with("story-2", "deploy")
