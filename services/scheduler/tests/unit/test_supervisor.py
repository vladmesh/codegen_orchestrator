"""Tests for pipeline supervisor — stuck detection, retry, and fail-fast.

Run-routing tests (DEPLOYING/TESTING stories) live in
`test_supervisor_run_routing.py`; shared DTO factories in `_run_routing_factories`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from _run_routing_factories import _make_repo, _make_story, _make_task
import pytest

from shared.contracts.dto.server import ServerDTO
from shared.tests.allocation_routing_cases import (
    REFUSAL_ROUTING_CASES,
    REFUSED_DEPLOY_MIN_DISK_MB,
    REFUSED_DEPLOY_REQUIRED_RAM_MB,
)
from shared.tests.server_admission_cases import (
    ADMISSION_CASES,
    admission_case_incidents,
    admission_case_server,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _provisioned_server() -> ServerDTO:
    """A managed host that finished provisioning — the only kind admission takes."""
    case = next(candidate for candidate in ADMISSION_CASES if candidate.admitted)
    return admission_case_server(case, last_health_check=datetime.now(UTC))


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

    @pytest.mark.asyncio
    async def test_capacity_failure_parks_without_spending_iteration(
        self, api_client, redis_client
    ):
        """A typed capacity failure enters the wait state instead of technical retry."""
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run_result import (
            AllocationFailureReason,
            EngineeringRunResult,
        )
        from src.tasks.task_dispatcher import supervise_failed_tasks

        task = _make_task(id="task-1", story_id="story-1", status="failed")
        api_client.get_tasks_by_status.return_value = [task]
        api_client.list_runs.return_value = [
            SimpleNamespace(
                result=EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED,
                    allocation_failure_reason=AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
                    allocation_required_ram_mb=768,
                    allocation_min_disk_mb=1024,
                )
            )
        ]
        api_client.get_project.return_value = SimpleNamespace(owner_id=42)

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result == {"retried": 0, "escalated": 0}
        api_client.transition_task.assert_awaited_once_with(
            "task-1", "waiting_resources", "supervisor"
        )
        api_client.update_task.assert_awaited_once()
        redis_client.publish_flat.assert_awaited_once()
        api_client.list_runs.assert_awaited_once_with(task_id="task-1", run_type="engineering")
        api_client.get_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_engineering_run_keeps_technical_retry_path(
        self, api_client, redis_client
    ):
        """A failed task without a run must still be retried instead of aborting the tick."""
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(id="task-1", story_id="story-1", status="failed", current_iteration=1)
        ]
        api_client.list_runs.return_value = []

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result == {"retried": 1, "escalated": 0}
        assert [call.args[1] for call in api_client.transition_task.call_args_list] == [
            "backlog",
            "todo",
        ]
        api_client.update_task.assert_awaited_once_with("task-1", {"current_iteration": 2})

    @pytest.mark.asyncio
    async def test_no_fresh_metrics_escalates_without_spending_an_iteration(
        self, api_client, redis_client
    ):
        """A fleet the platform cannot see is an operator's problem, not the code's.

        This replaces `test_no_fresh_metrics_keeps_technical_retry_path`, which
        asserted that the same refusal went back to the engineering worker. It
        cannot: the allocator refuses at the same point every time, so the retry
        spends the user's iteration budget on the platform's blind spot and ends
        in this queue anyway. Its other claim — that the owner never hears a
        capacity message for it — is kept below.
        """
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run_result import (
            AllocationFailureReason,
            EngineeringRunResult,
        )
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(id="task-1", story_id="story-1", status="failed")
        ]
        api_client.list_runs.return_value = [
            SimpleNamespace(
                result=EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED,
                    allocation_failure_reason=AllocationFailureReason.NO_FRESH_METRICS,
                )
            )
        ]

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as notify:
            result = await supervise_failed_tasks(api_client, redis_client)

        assert result == {"retried": 0, "escalated": 0}
        api_client.transition_task.assert_awaited_once_with(
            "task-1", "waiting_human_review", "supervisor"
        )
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        notify.assert_awaited_once()
        # No user-facing message: there is nothing for the owner to decide.
        redis_client.publish_flat.assert_not_awaited()
        api_client.update_task.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_impossible_capacity_escalates_without_retry(self, api_client, redis_client):
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run_result import (
            AllocationFailureReason,
            EngineeringRunResult,
        )
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(id="task-1", story_id="story-1", status="failed")
        ]
        api_client.list_runs.return_value = [
            SimpleNamespace(
                result=EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED,
                    allocation_failure_reason=AllocationFailureReason.IMPOSSIBLE_CAPACITY,
                )
            )
        ]
        api_client.get_project.return_value = SimpleNamespace(owner_id=42)

        with patch("src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock):
            result = await supervise_failed_tasks(api_client, redis_client)

        assert result == {"retried": 0, "escalated": 0}
        api_client.transition_task.assert_awaited_once_with(
            "task-1", "waiting_human_review", "supervisor"
        )
        # The action endpoint, not the status value: posting the status is a 404,
        # so the escalation used to reach nobody.
        api_client.transition_story.assert_awaited_once_with("story-1", "human-review")
        redis_client.publish_flat.assert_awaited_once()


class TestEngineeringRefusalRouting:
    """Each disposition gets its own behaviour on the engineering path.

    The expectations come from `shared.tests.allocation_routing_cases`, the same
    matrix the deploy suite drives, and each case carries its own — so two
    dispositions that start behaving identically fail here instead of being
    recorded as the contract.
    """

    @staticmethod
    def _failed_run(reason):
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run_result import EngineeringRunResult

        return SimpleNamespace(
            result=EngineeringRunResult(
                engineering_status=EngineeringStatus.FAILED,
                allocation_failure_reason=reason,
                allocation_required_ram_mb=REFUSED_DEPLOY_REQUIRED_RAM_MB,
                allocation_min_disk_mb=REFUSED_DEPLOY_MIN_DISK_MB,
            )
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", REFUSAL_ROUTING_CASES, ids=lambda case: case.reason.value)
    async def test_each_disposition_routes_the_way_the_matrix_says(
        self, api_client, redis_client, case
    ):
        from src.tasks.task_dispatcher import supervise_failed_tasks

        expected = case.engineering
        api_client.get_tasks_by_status.return_value = [
            _make_task(id="task-1", story_id="story-1", status="failed")
        ]
        api_client.list_runs.return_value = [self._failed_run(case.reason)]
        api_client.get_project.return_value = SimpleNamespace(owner_id=42)

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as notify:
            result = await supervise_failed_tasks(api_client, redis_client)

        # Handled here, so the caller's code-retry path never sees it.
        assert result == {"retried": 0, "escalated": 0}
        assert [call.args for call in api_client.transition_task.call_args_list] == [
            ("task-1", expected.task_status.value, "supervisor")
        ]
        if expected.story_action is None:
            api_client.transition_story.assert_not_awaited()
        else:
            api_client.transition_story.assert_awaited_once_with("story-1", expected.story_action)
        assert notify.await_count == (1 if expected.admin_alerted else 0)
        published = [call.args[1] for call in redis_client.publish_flat.call_args_list]
        assert [event["event"] for event in published] == (
            [] if expected.owner_event is None else [expected.owner_event]
        )
        # The story is never terminated by an allocation refusal.
        api_client.fail_story.assert_not_called()


class TestSuperviseWaitingResourceTasks:
    @pytest.mark.asyncio
    async def test_fresh_capacity_resumes_task_and_notifies_once(self, api_client, redis_client):
        from src.tasks.supervisor import supervise_waiting_resource_tasks

        task = _make_task(
            status="waiting_resources",
            failure_metadata={"allocation_required_ram_mb": 768, "allocation_min_disk_mb": 1024},
        )
        api_client.get_tasks_by_status.return_value = [task]
        api_client.get_servers.return_value = [_provisioned_server()]
        api_client.list_active_incidents.return_value = []
        api_client.get_applications.return_value = []
        api_client.get_project.return_value = SimpleNamespace(owner_id=42)

        result = await supervise_waiting_resource_tasks(api_client, redis_client)

        assert result == {"resumed": 1, "expired": 0}
        assert [call.args[1] for call in api_client.transition_task.call_args_list] == [
            "backlog",
            "todo",
        ]
        redis_client.publish_flat.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_resume_clears_failed_run_iteration_before_dispatch(
        self, api_client, redis_client
    ):
        """A resumed task must create a fresh run without spending an iteration."""
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run import RunStatus
        from shared.contracts.dto.run_result import AllocationFailureReason, EngineeringRunResult
        from src.tasks.supervisor import supervise_waiting_resource_tasks
        from src.tasks.task_dispatcher import dispatch_todo_tasks, supervise_failed_tasks

        task = _make_task(
            status="failed",
            story_id="story-1",
            current_iteration=1,
        )
        old_run = SimpleNamespace(
            id="eng-capacity-failed",
            run_metadata={"iteration": 1, "task_id": task.id},
            status=RunStatus.FAILED,
            result=EngineeringRunResult(
                engineering_status=EngineeringStatus.FAILED,
                allocation_failure_reason=AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
                allocation_required_ram_mb=768,
                allocation_min_disk_mb=1024,
            ),
        )
        api_client.get_tasks_by_status.side_effect = lambda status: (
            [task] if status in {"failed", "waiting_resources", "todo"} else []
        )
        api_client.list_runs.return_value = [old_run]
        api_client.get_servers.return_value = [_provisioned_server()]
        api_client.list_active_incidents.return_value = []
        api_client.get_applications.return_value = []
        api_client.get_project.return_value = SimpleNamespace(
            owner_id=42,
            status="active",
            config={"workspace_ready": True},
        )
        api_client.get_tasks_by_story.return_value = [task]

        async def update_run(run_id, data):
            assert run_id == old_run.id
            old_run.run_metadata = data["run_metadata"]

        async def update_task(_task_id, data):
            task.failure_metadata = data["failure_metadata"]

        api_client.update_run.side_effect = update_run
        api_client.update_task.side_effect = update_task

        parked = await supervise_failed_tasks(api_client, redis_client)
        resumed = await supervise_waiting_resource_tasks(api_client, redis_client)
        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert parked == {"retried": 0, "escalated": 0}
        assert resumed == {"resumed": 1, "expired": 0}
        assert dispatched == 1
        assert task.current_iteration == 1
        assert task.failure_metadata["resource_wait_started_at"]
        api_client.update_run.assert_awaited_once_with(
            "eng-capacity-failed",
            {"run_metadata": {"iteration": None, "task_id": "task-1"}},
        )
        api_client.create_run.assert_awaited_once()
        assert api_client.create_run.call_args.args[0]["run_metadata"]["iteration"] == 1

    @pytest.mark.asyncio
    async def test_reparking_preserves_original_resource_wait_start(self, api_client, redis_client):
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run_result import (
            AllocationFailureReason,
            EngineeringRunResult,
        )
        from src.tasks.task_dispatcher import supervise_failed_tasks

        started_at = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
        task = _make_task(
            status="failed",
            story_id="story-1",
            failure_metadata={"resource_wait_started_at": started_at},
        )
        api_client.get_tasks_by_status.return_value = [task]
        api_client.list_runs.return_value = [
            SimpleNamespace(
                result=EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED,
                    allocation_failure_reason=AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
                    allocation_required_ram_mb=768,
                    allocation_min_disk_mb=1024,
                )
            )
        ]

        await supervise_failed_tasks(api_client, redis_client)

        assert (
            api_client.update_task.call_args.args[1]["failure_metadata"]["resource_wait_started_at"]
            == started_at
        )
        redis_client.publish_flat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_wait_becomes_visible_human_review(self, api_client, redis_client):
        from src.tasks.supervisor import supervise_waiting_resource_tasks

        task = _make_task(
            status="waiting_resources",
            failure_metadata={
                "allocation_required_ram_mb": 768,
                "resource_wait_started_at": (datetime.now(UTC) - timedelta(minutes=61)).isoformat(),
            },
        )
        api_client.get_tasks_by_status.return_value = [task]

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as notify:
            result = await supervise_waiting_resource_tasks(api_client, redis_client)

        assert result == {"resumed": 0, "expired": 1}
        api_client.transition_task.assert_awaited_once_with(
            "task-1", "waiting_human_review", "supervisor"
        )
        api_client.create_task_event.assert_awaited_once()
        notify.assert_awaited_once()


class TestProvisioningAdmissionInResourceWait:
    """The wait may only release a task towards a host the allocator would take.

    The states come from `shared.tests.server_admission_cases`, the same table the
    allocator is checked against, so a rule that starts differing between the two
    admission paths fails here.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ADMISSION_CASES, ids=lambda case: case.name)
    async def test_wait_releases_exactly_the_shared_matrix(self, case, api_client, redis_client):
        from src.tasks.supervisor import supervise_waiting_resource_tasks

        now = datetime.now(UTC)
        task = _make_task(
            status="waiting_resources",
            failure_metadata={
                "allocation_required_ram_mb": 768,
                "allocation_min_disk_mb": 1024,
            },
        )
        api_client.get_tasks_by_status.return_value = [task]
        api_client.get_servers.return_value = [admission_case_server(case, last_health_check=now)]
        api_client.list_active_incidents.return_value = admission_case_incidents(
            case, detected_at=now
        )
        api_client.get_applications.return_value = []
        api_client.get_project.return_value = SimpleNamespace(owner_id=42)

        result = await supervise_waiting_resource_tasks(api_client, redis_client)

        assert result == {"resumed": 1 if case.admitted else 0, "expired": 0}

    @pytest.mark.asyncio
    async def test_unprovisioned_host_parks_the_task_as_infrastructure(
        self, api_client, redis_client
    ):
        """An unfinished machine is not the project's defect and not a shortage."""
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run_result import (
            AllocationFailureReason,
            EngineeringRunResult,
        )
        from src.tasks.task_dispatcher import supervise_failed_tasks

        task = _make_task(id="task-1", story_id="story-1", status="failed")
        api_client.get_tasks_by_status.return_value = [task]
        api_client.list_runs.return_value = [
            SimpleNamespace(
                result=EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED,
                    allocation_failure_reason=AllocationFailureReason.SERVER_NOT_PROVISIONED,
                    allocation_required_ram_mb=768,
                    allocation_min_disk_mb=1024,
                )
            )
        ]
        api_client.get_project.return_value = SimpleNamespace(owner_id=42)

        with patch(
            "src.tasks.supervisor.notify_admins_best_effort", new_callable=AsyncMock
        ) as notify:
            result = await supervise_failed_tasks(api_client, redis_client)

        # No retry, no escalation: the code was never the problem.
        assert result == {"retried": 0, "escalated": 0}
        api_client.transition_task.assert_awaited_once_with(
            "task-1", "waiting_resources", "supervisor"
        )
        api_client.transition_story.assert_not_awaited()
        notify.assert_not_awaited()
        published = redis_client.publish_flat.await_args.args[1]
        assert published["event"] == "task_waiting_infrastructure"
        assert "capacity" not in published["text"]

    @pytest.mark.asyncio
    async def test_capacity_shortage_keeps_its_own_user_message(self, api_client, redis_client):
        """The two waits must stay distinguishable to the owner, not just in logs."""
        from shared.contracts.dto.engineering import EngineeringStatus
        from shared.contracts.dto.run_result import (
            AllocationFailureReason,
            EngineeringRunResult,
        )
        from src.tasks.task_dispatcher import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _make_task(id="task-1", story_id="story-1", status="failed")
        ]
        api_client.list_runs.return_value = [
            SimpleNamespace(
                result=EngineeringRunResult(
                    engineering_status=EngineeringStatus.FAILED,
                    allocation_failure_reason=AllocationFailureReason.INSUFFICIENT_FREE_MEMORY,
                    allocation_required_ram_mb=768,
                    allocation_min_disk_mb=1024,
                )
            )
        ]
        api_client.get_project.return_value = SimpleNamespace(owner_id=42)

        await supervise_failed_tasks(api_client, redis_client)

        published = redis_client.publish_flat.await_args.args[1]
        assert published["event"] == "task_waiting_resources"


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
