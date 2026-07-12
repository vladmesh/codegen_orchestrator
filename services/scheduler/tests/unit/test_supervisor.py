"""Tests for pipeline supervisor — stuck detection, retry, and fail-fast."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

from pydantic import ValidationError
import pytest

from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.run import RunDTO, RunStatus, RunType
from shared.contracts.dto.story import StoryDTO
from shared.contracts.dto.task import TaskDTO
from shared.contracts.queues.deploy import DeployOutcome
from shared.contracts.queues.qa import QAOutcome

# ---------------------------------------------------------------------------
# Factory helpers — build DTO instances with sensible defaults
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _make_story(
    *,
    id: str = "story-1",
    project_id: str = "00000000-0000-0000-0000-000000000001",
    title: str = "Test Story",
    status: str = "created",
    priority: int = 0,
    created_by: str = "system",
    type: str = "product",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    **kwargs,
) -> StoryDTO:
    return StoryDTO(
        id=id,
        project_id=UUID(project_id),
        title=title,
        status=status,
        priority=priority,
        created_by=created_by,
        type=type,
        created_at=created_at or _NOW,
        updated_at=updated_at,
        **kwargs,
    )


def _make_task(**overrides) -> TaskDTO:
    defaults = {
        "id": "task-1",
        "project_id": UUID("00000000-0000-0000-0000-000000000001"),
        "type": "feature",
        "title": "Test Task",
        "status": "todo",
        "priority": 0,
        "current_iteration": 0,
        "max_iterations": 3,
        "created_by": "system",
        "story_id": None,
        "created_at": _NOW,
        "updated_at": None,
    }
    # Allow project_id as string for convenience
    if "project_id" in overrides and isinstance(overrides["project_id"], str):
        overrides["project_id"] = UUID(overrides["project_id"])
    defaults.update(overrides)
    return TaskDTO(**defaults)


def _make_repo(
    *,
    id: str = "repo-1",
    project_id: str = "00000000-0000-0000-0000-000000000001",
    name: str = "test-project",
    git_url: str = "https://github.com/org/test-project",
    role: str = "primary",
    visibility: str = "private",
    is_managed: bool = True,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> RepositoryDTO:
    return RepositoryDTO(
        id=id,
        project_id=UUID(project_id),
        name=name,
        git_url=git_url,
        role=role,
        visibility=visibility,
        is_managed=is_managed,
        created_at=created_at or _NOW,
        updated_at=updated_at,
    )


def _make_run(
    *,
    id: str = "deploy-1",
    project_id: str = "00000000-0000-0000-0000-000000000001",
    type: str = RunType.DEPLOY,
    status: str = RunStatus.COMPLETED,
    story_id: str | None = "story-1",
    result: dict | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> RunDTO:
    return RunDTO(
        id=id,
        project_id=project_id,
        type=type,
        status=status,
        story_id=story_id,
        result=result,
        created_at=created_at or _NOW,
        updated_at=updated_at,
    )


def _invalid_result_error(run_type: str) -> ValidationError:
    """A real ValidationError from a run whose result belongs to another type."""
    other = "qa_outcome" if run_type == "deploy" else "deploy_outcome"
    try:
        RunDTO.model_validate(
            {
                "id": "bad-run",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "type": run_type,
                "status": "failed",
                "result": {other: "passed"},
                "created_at": _NOW.isoformat(),
            }
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


def _terminal_no_result_error(run_type: str) -> ValidationError:
    """A real ValidationError from a terminal run that lost its result."""
    try:
        RunDTO.model_validate(
            {
                "id": "no-result-run",
                "project_id": "00000000-0000-0000-0000-000000000001",
                "type": run_type,
                "status": "completed",
                "result": None,
                "created_at": _NOW.isoformat(),
            }
        )
    except ValidationError as exc:
        return exc
    raise AssertionError("expected ValidationError")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client():
    client = AsyncMock()
    return client


@pytest.fixture
def redis_client():
    client = AsyncMock()
    client.publish_message = AsyncMock()
    client.publish_flat = AsyncMock()
    client.publish = AsyncMock()
    client.redis = AsyncMock()
    client.redis.hget = AsyncMock(return_value=None)  # No story worker by default
    client.redis.hdel = AsyncMock()
    # _redis is used by supervise_stuck_stories for retry counter persistence
    client._redis = AsyncMock()
    client._redis.get = AsyncMock(return_value=None)  # No retries by default
    client._redis.set = AsyncMock()
    client._redis.delete = AsyncMock()
    return client


class TestSuperviseStuckStories:
    """Detect stories stuck in 'created' and retry architect or fail."""

    @pytest.mark.asyncio
    async def test_retries_stuck_story(self, api_client, redis_client):
        """Story stuck in created > threshold -> republish to architect:queue."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1", project_id="00000000-0000-0000-0000-000000000001", created_at=old
                )
            ]
            if status == "created"
            else []  # no in_progress stories
        )
        # No tasks yet = architect hasn't run
        api_client.get_tasks_by_story.return_value = []
        # No previous retry events
        api_client.get_task_events.side_effect = []

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 1
        assert result["failed"] == 0
        redis_client.publish_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_recent_story(self, api_client, redis_client):
        """Story created recently -> no action."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        recent = datetime.now(UTC) - timedelta(minutes=1)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1",
                    project_id="00000000-0000-0000-0000-000000000001",
                    created_at=recent,
                )
            ]
            if status == "created"
            else []
        )

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_story_with_tasks(self, api_client, redis_client):
        """Story in created but has tasks -> architect ran, skip."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1", project_id="00000000-0000-0000-0000-000000000001", created_at=old
                )
            ]
            if status == "created"
            else []
        )
        api_client.get_tasks_by_story.return_value = [_make_task(id="task-1")]

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        redis_client.publish_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_fails_story_after_max_retries(self, api_client, redis_client):
        """Story retried 3 times -> fail the story."""
        from src.tasks.supervisor import _max_architect_retries
        from src.tasks.task_dispatcher import supervise_stuck_stories

        max_retries = _max_architect_retries()
        old_enough = datetime.now(UTC) - timedelta(minutes=10 * (max_retries + 1))
        old = datetime.now(UTC) - timedelta(minutes=10)
        api_client.get_stories_by_status.side_effect = lambda status: (
            [
                _make_story(
                    id="story-1",
                    project_id="00000000-0000-0000-0000-000000000001",
                    created_at=old_enough,
                    updated_at=old,
                )
            ]
            if status == "created"
            else []  # no in_progress stories
        )
        api_client.get_tasks_by_story.return_value = []
        api_client.fail_story.return_value = {}

        # Simulate retry count already at max in Redis
        redis_client._redis.get.return_value = str(max_retries)

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_skips_created_story_when_project_has_active(self, api_client, redis_client):
        """Story stuck in created but project has an in_progress story -> skip."""
        from src.tasks.task_dispatcher import supervise_stuck_stories

        old = datetime.now(UTC) - timedelta(minutes=10)
        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.side_effect = lambda status: (
            [_make_story(id="story-queued", project_id=proj_id, created_at=old)]
            if status == "created"
            else [_make_story(id="story-active", project_id=proj_id, status="in_progress")]
        )
        api_client.get_tasks_by_story.return_value = []

        result = await supervise_stuck_stories(api_client, redis_client)

        assert result["retried"] == 0
        assert result["failed"] == 0
        redis_client.publish_message.assert_not_called()


class TestCompleteStoriesTriggersNext:
    """After completing a story, trigger the next queued story for the same project."""

    @pytest.mark.asyncio
    async def test_triggers_next_created_story(self, api_client, redis_client):
        """Story completed -> next created story for same project published to architect."""
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.side_effect = lambda status: (
            [_make_story(id="story-done", project_id=proj_id, status="in_progress")]
            if status == "in_progress"
            else [
                _make_story(
                    id="story-next",
                    project_id=proj_id,
                    status="created",
                    priority=0,
                    created_at=datetime.now(UTC),
                )
            ]
        )
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-done", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            completed = await complete_stories(api_client, redis_client)

        assert completed == 1
        # Should publish architect message for next story
        from shared.queues import ARCHITECT_QUEUE

        arch_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ARCHITECT_QUEUE
        ]
        assert len(arch_calls) == 1
        assert arch_calls[0][0][1].story_id == "story-next"

    @pytest.mark.asyncio
    async def test_no_next_story_when_none_queued(self, api_client, redis_client):
        """Story completed but no created stories for project -> no architect trigger."""
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.side_effect = lambda status: (
            [_make_story(id="story-done", project_id=proj_id, status="in_progress")]
            if status == "in_progress"
            else []
        )
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-done", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        from shared.queues import ARCHITECT_QUEUE

        arch_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ARCHITECT_QUEUE
        ]
        assert len(arch_calls) == 0


class TestSuperviseFailedTasks:
    """Detect failed tasks and retry or escalate to WHR."""

    @pytest.mark.asyncio
    async def test_retries_failed_task(self, api_client, redis_client):
        """Failed task with iterations left -> reopen to todo."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="failed",
                current_iteration=0,
                max_iterations=3,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.update_task.return_value = {}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 1
        # Should transition: failed -> backlog -> todo
        calls = api_client.transition_task.call_args_list
        assert len(calls) == 2  # noqa: PLR2004
        assert calls[0].args == ("task-1", "backlog", "supervisor")
        assert calls[1].args == ("task-1", "todo", "supervisor")
        # Should increment current_iteration
        api_client.update_task.assert_called_once_with("task-1", {"current_iteration": 1})

    @pytest.mark.asyncio
    async def test_escalates_to_whr_when_retries_exhausted(self, api_client, redis_client):
        """Failed task at max iterations -> escalate to waiting_human_review."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="failed",
                current_iteration=3,
                max_iterations=3,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.transition_story.return_value = {}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["escalated"] == 1
        # Task should be transitioned to WHR
        api_client.transition_task.assert_called_once_with(
            "task-1", "waiting_human_review", "supervisor"
        )
        # Story should also be transitioned to WHR
        api_client.transition_story.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_task_without_story(self, api_client, redis_client):
        """Failed task without story_id -> skip (standalone task)."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id=None,
                status="failed",
                current_iteration=0,
                max_iterations=3,
            )
        ]

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 0
        assert result["escalated"] == 0
        api_client.transition_task.assert_not_called()


class TestSuperviseStuckTasks:
    """Detect tasks stuck in in_dev and fail them."""

    @pytest.mark.asyncio
    async def test_fails_stuck_in_dev_task(self, api_client, redis_client):
        """Task in in_dev > threshold -> transition to failed."""
        from src.tasks.task_dispatcher import supervise_stuck_tasks

        old = datetime.now(UTC) - timedelta(minutes=45)
        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="in_dev",
                updated_at=old,
            )
        ]
        api_client.transition_task.return_value = {}

        result = await supervise_stuck_tasks(api_client, redis_client)

        assert result["timed_out"] == 1
        api_client.transition_task.assert_called_once_with("task-1", "failed", "supervisor")

    @pytest.mark.asyncio
    async def test_skips_recent_in_dev_task(self, api_client, redis_client):
        """Task recently updated -> no action."""
        from src.tasks.task_dispatcher import supervise_stuck_tasks

        recent = datetime.now(UTC) - timedelta(minutes=5)
        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="in_dev",
                updated_at=recent,
            )
        ]

        result = await supervise_stuck_tasks(api_client, redis_client)

        assert result["timed_out"] == 0
        api_client.transition_task.assert_not_called()


class TestStoryWorkerCleanup:
    """Cleanup story workers on story complete/fail."""

    @pytest.mark.asyncio
    async def test_cleanup_on_story_complete(self, api_client, redis_client):
        """Story completed -> worker container deleted, registry cleared."""
        from shared.queues import STORY_WORKERS_KEY
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", project_id=proj_id, status="in_progress")
        ]
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-1", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        # Story has a worker registered
        redis_client.redis.hget.return_value = b"dev-story-worker"

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        # Should lookup worker
        redis_client.redis.hget.assert_called_with(STORY_WORKERS_KEY, "story-1")
        # Should send delete command
        redis_client.publish.assert_called_once()
        # Should clear registry
        redis_client.redis.hdel.assert_called_with(STORY_WORKERS_KEY, "story-1")

    @pytest.mark.asyncio
    async def test_no_cleanup_when_no_worker(self, api_client, redis_client):
        """Story completed but no worker registered -> no cleanup."""
        from src.tasks.task_dispatcher import complete_stories

        proj_id = "00000000-0000-0000-0000-000000000001"
        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", project_id=proj_id, status="in_progress")
        ]
        api_client.get_tasks_by_story.return_value = [
            _make_task(id="task-1", status="done"),
        ]
        api_client.transition_story.return_value = {}
        api_client.get_story.return_value = _make_story(id="story-1", project_id=proj_id)
        api_client.get_primary_repository.return_value = _make_repo(
            git_url="https://github.com/org/test-project",
        )

        # No worker registered
        redis_client.redis.hget.return_value = None

        mock_github = AsyncMock()
        mock_github.create_pull_request.return_value = {
            "number": 1,
            "node_id": "PR_node1",
        }
        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            await complete_stories(api_client, redis_client)

        # Should not send delete command or clear registry
        redis_client.publish.assert_not_called()
        redis_client.redis.hdel.assert_not_called()

    @pytest.mark.asyncio
    async def test_escalation_transitions_story_to_whr(self, api_client, redis_client):
        """Task retries exhausted -> story transitioned to WHR (not failed)."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(
                id="task-1",
                story_id="story-1",
                status="failed",
                current_iteration=3,
                max_iterations=3,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.transition_story.return_value = {}

        await supervise_failed_tasks(api_client, redis_client)

        # Story should NOT be failed — just transitioned to WHR
        api_client.fail_story.assert_not_called()
        api_client.transition_story.assert_called_once()


class TestSuperviseDeployingStories:
    """Poll DEPLOYING stories and route based on deploy run outcome."""

    @pytest.mark.asyncio
    async def test_success_transitions_to_testing(self, api_client, redis_client):
        """SUCCESS outcome → story TESTING, QA message published."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            result={
                "deploy_outcome": DeployOutcome.SUCCESS.value,
                "deployed_url": "https://example.com",
                "application_id": 42,
            },
        )
        api_client.transition_story.return_value = {}

        api_client.create_run.return_value = {"id": "qa-run-1"}

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["tested"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "test")

        # QA run should be created
        api_client.create_run.assert_called_once()
        run_data = api_client.create_run.call_args[0][0]
        assert run_data["type"] == RunType.QA.value
        assert run_data["story_id"] == "story-1"

        # QA message should be published with run_id
        from shared.queues import QA_QUEUE

        qa_calls = [c for c in redis_client.publish_message.call_args_list if c[0][0] == QA_QUEUE]
        assert len(qa_calls) == 1
        qa_msg = qa_calls[0][0][1]
        assert qa_msg.deployed_url == "https://example.com"
        assert qa_msg.application_id == 42
        assert qa_msg.run_id  # run_id must be set

    @pytest.mark.asyncio
    async def test_give_up_fails_story(self, api_client, redis_client):
        """GIVE_UP outcome → story FAILED, admin notified."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={
                "deploy_outcome": DeployOutcome.GIVE_UP.value,
                "error_details": "port already allocated",
            },
        )
        api_client.fail_story.return_value = {}

        with patch("src.tasks.supervisor.notify_admins", new_callable=AsyncMock) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_code_fix_redispatches_to_engineering(self, api_client, redis_client):
        """CODE_FIX outcome → story IN_PROGRESS, engineering message published."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={
                "deploy_outcome": DeployOutcome.CODE_FIX.value,
                "error_details": "ImportError: no module",
                "deploy_fix_attempt": 0,
            },
        )
        api_client.transition_story.return_value = {}
        api_client.create_run.return_value = {}

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["redispatched"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "start")

        from shared.queues import ENGINEERING_QUEUE

        eng_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == ENGINEERING_QUEUE
        ]
        assert len(eng_calls) == 1

    @pytest.mark.asyncio
    async def test_retry_republishes_deploy(self, api_client, redis_client):
        """RETRY outcome → new deploy run created, deploy message published."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={"deploy_outcome": DeployOutcome.RETRY.value},
        )
        api_client.create_run.return_value = {}
        # First retry
        redis_client._redis.incr.return_value = 1

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result["retried"] == 1
        from shared.queues import DEPLOY_QUEUE

        deploy_calls = [
            c for c in redis_client.publish_message.call_args_list if c[0][0] == DEPLOY_QUEUE
        ]
        assert len(deploy_calls) == 1

    @pytest.mark.asyncio
    async def test_retry_exhausted_fails_story(self, api_client, redis_client):
        """RETRY with max retries exceeded → story FAILED."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.FAILED,
            result={"deploy_outcome": DeployOutcome.RETRY.value},
        )
        api_client.fail_story.return_value = {}
        # Max retries hit
        redis_client._redis.incr.return_value = 3  # default max is 3

        with patch("src.tasks.supervisor.notify_admins", new_callable=AsyncMock):
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_skips_running_deploys(self, api_client, redis_client):
        """RUNNING deploy → skip (still in progress)."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.RUNNING, result=None
        )

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result == {"tested": 0, "retried": 0, "redispatched": 0, "failed": 0}
        api_client.transition_story.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_story_with_no_runs(self, api_client, redis_client):
        """DEPLOYING story with no runs → skip."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = None

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result == {"tested": 0, "retried": 0, "redispatched": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_invalid_deploy_result_fails_story(self, api_client, redis_client):
        """Unparseable deploy result → story failed once, admin notified, no loop."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.side_effect = _invalid_result_error("deploy")
        api_client.fail_story.return_value = {}

        with patch("src.tasks.supervisor.notify_admins", new_callable=AsyncMock) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminal_deploy_without_result_fails_story(self, api_client, redis_client):
        """A terminal deploy run that lost its result routes to a visible failure, not a skip."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.side_effect = _terminal_no_result_error("deploy")
        api_client.fail_story.return_value = {}

        with patch("src.tasks.supervisor.notify_admins", new_callable=AsyncMock) as mock_notify:
            result = await supervise_deploying_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancelled_deploy_is_skipped(self, api_client, redis_client):
        """A CANCELLED (superseded) deploy run has no result → skip, don't fail the story."""
        from src.tasks.supervisor import supervise_deploying_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="deploying")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            status=RunStatus.CANCELLED, result=None
        )

        result = await supervise_deploying_stories(api_client, redis_client)

        assert result == {"tested": 0, "retried": 0, "redispatched": 0, "failed": 0}
        api_client.fail_story.assert_not_called()
        api_client.transition_story.assert_not_called()


class TestSuperviseTestingStories:
    """Poll TESTING stories and route based on QA run outcome."""

    @pytest.mark.asyncio
    async def test_passed_completes_story(self, api_client, redis_client):
        """PASSED outcome → story COMPLETED."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.PASSED.value,
                "deployed_url": "https://example.com",
            },
        )
        api_client.transition_story.return_value = {}

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["completed"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "complete")

    @pytest.mark.asyncio
    async def test_failed_creates_fix_task_and_redispatches(self, api_client, redis_client):
        """FAILED outcome → fix task created, story back to IN_PROGRESS, engineering redispatch."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.FAILED.value,
                "summary": "Weather endpoint broken",
                "failed_checks": [{"name": "weather", "detail": "404"}],
                "qa_attempt": 0,
            },
        )
        api_client.transition_story.return_value = {}
        api_client.create_task.return_value = {"id": "task-fix-1"}

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["redispatched"] == 1
        api_client.transition_story.assert_called_once_with("story-1", "start")
        api_client.create_task.assert_called_once()
        task_data = api_client.create_task.call_args[0][0]
        assert task_data["story_id"] == "story-1"
        assert task_data["status"] == "todo"
        assert "weather" in task_data["description"].lower()

    @pytest.mark.asyncio
    async def test_exhausted_fails_story(self, api_client, redis_client):
        """EXHAUSTED outcome → story FAILED."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.EXHAUSTED.value,
                "summary": "Still broken after 2 attempts",
                "qa_attempt": 2,
            },
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_error_fails_story(self, api_client, redis_client):
        """ERROR outcome → story FAILED."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1",
            type=RunType.QA,
            result={
                "qa_outcome": QAOutcome.ERROR.value,
                "error": "bot_username missing",
            },
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")

    @pytest.mark.asyncio
    async def test_skips_running_qa(self, api_client, redis_client):
        """QA run still RUNNING → skip, no action."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = _make_run(
            id="qa-1", type=RunType.QA, status=RunStatus.RUNNING, result=None
        )

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {"completed": 0, "redispatched": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_no_testing_stories(self, api_client, redis_client):
        """No TESTING stories → zero counts."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = []

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {"completed": 0, "redispatched": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_no_qa_runs_skips(self, api_client, redis_client):
        """TESTING story with no QA runs → skip."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.return_value = None

        result = await supervise_testing_stories(api_client, redis_client)

        assert result == {"completed": 0, "redispatched": 0, "failed": 0}

    @pytest.mark.asyncio
    async def test_invalid_qa_result_fails_story(self, api_client, redis_client):
        """Unparseable QA result → story failed once, admin notified, no loop."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.side_effect = _invalid_result_error("qa")
        api_client.fail_story.return_value = {}

        with patch("src.tasks.supervisor.notify_admins", new_callable=AsyncMock) as mock_notify:
            result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminal_qa_without_result_fails_story(self, api_client, redis_client):
        """A terminal QA run that lost its result routes to a visible failure, not a skip."""
        from src.tasks.supervisor import supervise_testing_stories

        api_client.get_stories_by_status.return_value = [
            _make_story(id="story-1", status="testing")
        ]
        api_client.get_latest_run_by_story.side_effect = _terminal_no_result_error("qa")
        api_client.fail_story.return_value = {}

        with patch("src.tasks.supervisor.notify_admins", new_callable=AsyncMock) as mock_notify:
            result = await supervise_testing_stories(api_client, redis_client)

        assert result["failed"] == 1
        api_client.fail_story.assert_called_once_with("story-1")
        mock_notify.assert_called_once()
