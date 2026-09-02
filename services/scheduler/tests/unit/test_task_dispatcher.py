"""Tests for task dispatcher — dispatches todo tasks and completes stories."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from shared.contracts.dto.engineering_budget_policy import (
    EngineeringBudgetAdmissionOutcome,
    EngineeringBudgetAdmissionRead,
)
from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchOutcome,
    EngineeringDispatchRead,
    EngineeringDispatchRefusal,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.repository import RepositoryDTO
from shared.contracts.dto.story import WAITING_ON_BY_STATUS, StoryDTO, StoryStatus
from shared.contracts.dto.task import TaskDTO, TaskEventDTO
from shared.contracts.dto.work_admission import (
    PaidRunStartRead,
    WorkAdmissionOutcome,
    WorkAdmissionRead,
)
from shared.contracts.vocab import ActionType

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
    # Required on the DTO, and implied by the status the story sits on.
    defaults.setdefault("waiting_on", WAITING_ON_BY_STATUS[StoryStatus(defaults["status"])].value)
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


def _admission(
    outcome: EngineeringBudgetAdmissionOutcome = EngineeringBudgetAdmissionOutcome.ADMITTED,
) -> EngineeringBudgetAdmissionRead:
    return EngineeringBudgetAdmissionRead(
        attempt_id="eng-budget-test",
        user_id=1,
        outcome=outcome,
        reservation_microusd=10,
        known_spend_microusd=0,
        active_held_microusd=10 if outcome is EngineeringBudgetAdmissionOutcome.ADMITTED else 0,
        available_microusd=90,
        reservation_state=(
            "active" if outcome is EngineeringBudgetAdmissionOutcome.ADMITTED else None
        ),
    )


def _admitted(run_id: str = "eng-test") -> EngineeringDispatchRead:
    """The admission point admitting a dispatch: the attempt exists and is held."""
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.ADMITTED,
        run_id=run_id,
        initiating_run_id="live-run-1",
        paid_work=PaidRunStartRead(
            admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.ADMITTED), run_id=run_id
        ),
    )


def _refused(reason: EngineeringDispatchRefusal) -> EngineeringDispatchRead:
    """A refusal decided before the paid gate: nothing was created, nothing counted."""
    return EngineeringDispatchRead(outcome=EngineeringDispatchOutcome.REFUSED, reason=reason)


def _paid_refusal(
    reason: EngineeringDispatchRefusal,
    *,
    budget: EngineeringBudgetAdmissionRead | None = None,
    message: str | None = None,
    run_id: str = "eng-test",
) -> EngineeringDispatchRead:
    """A refusal from the paid gate, carrying the paid decision it wraps."""
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.REFUSED,
        reason=reason,
        run_id=run_id,
        initiating_run_id="live-run-1",
        paid_work=PaidRunStartRead(
            admission=WorkAdmissionRead(outcome=WorkAdmissionOutcome.DENIED, message=message),
            engineering_budget=budget,
        ),
    )


def _repair(repair: EngineeringDispatchRepair, run_id: str = "eng-abc") -> EngineeringDispatchRead:
    """A prior attempt this task still owes transition work for."""
    return EngineeringDispatchRead(
        outcome=EngineeringDispatchOutcome.REPAIR,
        repair=repair,
        reason=(
            EngineeringDispatchRefusal.LIVE_ATTEMPT_IN_FLIGHT
            if repair is EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
            else None
        ),
        run_id=run_id,
        initiating_run_id="live-run-1",
    )


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
    # The run this project's work belongs to — what the dispatcher puts on the
    # message so the worker it leads to is owned by it.
    project_mock.initiating_run_id = "live-run-1"
    client.get_project.return_value = project_mock
    # Default: project has existing applications (feature deploy)
    client.get_applications_by_project.return_value = [{"id": 1, "status": "running"}]
    # Default: no live engineering run left over from a previous tick
    client.list_runs.return_value = []
    client.admit_engineering_budget.return_value = _admission()
    # The one question dispatch asks. Admitted by default: every condition that
    # used to be answered from the mocks above now lives behind this call.
    client.admit_engineering_dispatch.return_value = _admitted()
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
    async def test_message_carries_the_run_the_project_was_created_for(
        self, api_client, redis_client
    ):
        """The message owns the work by the initiating run, not by this attempt.

        The dispatcher creates an engineering Run row per attempt, and the old
        contract had nothing else on the message to own a worker by. The run
        that *asked* for the work is a different, longer-lived identity, read
        off the project where its initiator wrote it; the attempt travels
        beside it as `task_id`, never as its substitute.
        """
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
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        decision = api_client.admit_engineering_dispatch.return_value
        msg = redis_client.publish_message.call_args[0][1]
        assert msg.initiating_run_id == decision.initiating_run_id
        assert msg.task_id == decision.run_id
        assert msg.task_id != msg.initiating_run_id

    #: Every refusal the admission point reaches before anything is counted.
    #: Each of these was an inline condition of `dispatch_todo_tasks` with a test
    #: of its own here — a project that predates run ownership, an
    #: unresolved blocker, a draft project, a workspace that is not ready, a busy
    #: story, a story with a sibling in human review, and the internal project.
    #: What the dispatcher owes them is now one behaviour, so they are pinned
    #: here as one property; which *state* produces which reason is pinned in
    #: services/api/tests/service/test_engineering_dispatch_admission.py, where
    #: the conditions moved.
    _UNCOUNTED_REFUSALS = [
        EngineeringDispatchRefusal.TASK_NOT_DISPATCHABLE,
        EngineeringDispatchRefusal.INTERNAL_PROJECT,
        EngineeringDispatchRefusal.BLOCKER_UNRESOLVED,
        EngineeringDispatchRefusal.PROJECT_HAS_NO_INITIATING_RUN,
        EngineeringDispatchRefusal.PROJECT_NOT_SCAFFOLDED,
        EngineeringDispatchRefusal.WORKSPACE_NOT_READY,
        EngineeringDispatchRefusal.STORY_BUSY,
        EngineeringDispatchRefusal.STORY_WAITING_HUMAN_REVIEW,
    ]

    @pytest.mark.parametrize("reason", _UNCOUNTED_REFUSALS)
    @pytest.mark.asyncio
    async def test_a_refusal_that_counted_nothing_leaves_the_task_untouched(
        self, api_client, redis_client, reason
    ):
        """No message, no transition, no compensation — and a later tick may retry.

        These refusals are decided before the paid gate, so no attempt exists to
        release and the task keeps its place in the todo queue.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo")
        ]
        api_client.admit_engineering_dispatch.return_value = _refused(reason)

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_not_called()
        api_client.transition_story.assert_not_called()
        api_client.abort_paid_run_pre_handoff.assert_not_called()

    @pytest.mark.asyncio
    async def test_every_todo_task_is_asked_about_by_id(self, api_client, redis_client):
        """The dispatcher selects candidates and asks; it decides nothing itself."""
        from shared.contracts.dto.engineering_dispatch import EngineeringDispatchCommand
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo"),
            _task(id="task-2", project_id=PROJ_ID, story_id="story-2", status="todo"),
        ]
        api_client.get_task_events.return_value = []

        await dispatch_todo_tasks(api_client, redis_client)

        assert [call.args[0] for call in api_client.admit_engineering_dispatch.await_args_list] == [
            EngineeringDispatchCommand(task_id="task-1"),
            EngineeringDispatchCommand(task_id="task-2"),
        ]

    @pytest.mark.asyncio
    async def test_an_unanswered_question_dispatches_nothing(self, api_client, redis_client):
        """A failed admission call decided nothing, so nothing was counted or owed."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(id="task-1", project_id=PROJ_ID, story_id="story-1", status="todo")
        ]
        api_client.admit_engineering_dispatch.side_effect = RuntimeError("API unavailable")

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_not_called()
        api_client.abort_paid_run_pre_handoff.assert_not_called()

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
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # The attempt was created by the admission point, which was asked about
        # this task and nothing else.
        api_client.admit_engineering_dispatch.assert_awaited_once()
        assert api_client.admit_engineering_dispatch.await_args.args[0].task_id == "task-1"
        api_client.start_paid_run.assert_not_called()

        # Should publish to engineering queue
        redis_client.publish_message.assert_called_once()
        assert redis_client.publish_message.call_args[0][1].task_id == "eng-test"

        # Should transition task to in_dev
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    @pytest.mark.asyncio
    async def test_budget_denial_moves_task_to_human_review_without_a_retry(
        self, api_client, redis_client
    ):
        """Denial is terminal for automatic dispatch until a human resumes it."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        state = {
            "task": _task(
                id="task-1",
                project_id=PROJ_ID,
                story_id="story-1",
                status="todo",
            )
        }

        async def list_todo_tasks(*_args, **_kwargs):
            return [state["task"]] if state["task"].status == "todo" else []

        async def apply_transition(task_id, status, *_args, **_kwargs):
            assert task_id == "task-1"
            state["task"] = state["task"].model_copy(update={"status": status})
            return {}

        api_client.get_tasks_by_status.side_effect = list_todo_tasks
        api_client.transition_task.side_effect = apply_transition
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _paid_refusal(
            EngineeringDispatchRefusal.ENGINEERING_BUDGET_DENIED,
            budget=_admission(EngineeringBudgetAdmissionOutcome.DENIED),
        )

        assert await dispatch_todo_tasks(api_client, redis_client) == 0
        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        api_client.admit_engineering_dispatch.assert_awaited_once()
        redis_client.publish_message.assert_not_called()
        first, second = api_client.transition_task.await_args_list
        assert first.args == ("task-1", "in_dev", "dispatcher")
        assert second.args == ("task-1", "waiting_human_review", "dispatcher")
        assert second.kwargs["details"] == {
            "reason": "engineering_budget_denied",
            "attempt_id": "eng-budget-test",
            "known_spend_microusd": 0,
            "active_held_microusd": 0,
            "available_microusd": 90,
        }
        assert state["task"].status == "waiting_human_review"

    @pytest.mark.asyncio
    async def test_dispatches_refactor_task_as_feature_action(self, api_client, redis_client):
        """Planning refactors use the engineering feature action."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                type="refactor",
                project_id=PROJ_ID,
                story_id="story-1",
            )
        ]
        api_client.get_task_events.return_value = []

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.action is ActionType.FEATURE
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

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
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        api_client.admit_engineering_dispatch.assert_awaited_once()
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
        api_client.transition_task.return_value = {}

        await dispatch_todo_tasks(api_client, redis_client)

        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.story_id is None

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
        api_client.transition_task.return_value = {}
        api_client.get_story.return_value = _story(id="story-1", project_id=PROJ_ID)

        await dispatch_todo_tasks(api_client, redis_client)

        # Should dispatch — normal failure doesn't block siblings
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
        api_client.transition_task.return_value = {}

        await dispatch_todo_tasks(api_client, redis_client)

        redis_client.publish_message.assert_called_once()
        eng_msg = redis_client.publish_message.call_args[0][1]
        assert eng_msg.branch is None


class TestDispatchPartialFailure:
    """Dispatch is three non-atomic steps; a failure must not leave debris."""

    @staticmethod
    def _todo_task(**overrides):
        return _task(
            id="task-1",
            title="Add user model",
            description="Create User SQLAlchemy model",
            type="feature",
            project_id=PROJ_ID,
            story_id="story-1",
            status="todo",
            **overrides,
        )

    @pytest.mark.asyncio
    async def test_publish_failure_keeps_the_run_owned_for_recovery(self, api_client, redis_client):
        """A lost publish response is not evidence that a worker did not start."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        redis_client.publish_message.side_effect = RuntimeError("redis is down")

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 0
        api_client.abort_paid_run_pre_handoff.assert_not_awaited()
        # The task stays in todo until unfinished-run recovery confirms ownership.
        api_client.transition_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_recipient_failure_releases_the_pre_handoff_reservation(
        self, api_client, redis_client, monkeypatch
    ):
        """Recipient resolution is before queue handoff and must compensate its hold."""
        from src.tasks import task_dispatcher

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        monkeypatch.setattr(
            task_dispatcher,
            "resolve_project_recipient",
            AsyncMock(side_effect=RuntimeError("recipient unavailable")),
        )

        assert await task_dispatcher.dispatch_todo_tasks(api_client, redis_client) == 0

        api_client.abort_paid_run_pre_handoff.assert_awaited_once()
        assert (
            api_client.abort_paid_run_pre_handoff.await_args.args[0]
            == api_client.admit_engineering_dispatch.return_value.run_id
        )
        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_transition_failure_is_retried_once(self, api_client, redis_client):
        """A flaky transition is retried in the same tick, without republishing."""
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.transition_task.side_effect = [RuntimeError("api hiccup"), {}]

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        api_client.admit_engineering_dispatch.assert_awaited_once()
        redis_client.publish_message.assert_called_once()
        assert api_client.transition_task.call_count == 2

    @staticmethod
    def _prior_run_from_patch(run_id: str, patch: dict):
        """Rebuild the run a compensating PATCH leaves in the API."""
        from _run_routing_factories import _make_run

        from shared.contracts.dto.run import RunType

        return _make_run(
            id=run_id,
            type=RunType.ENGINEERING,
            status=patch["status"],
            result=patch["result"],
            run_metadata=patch["run_metadata"],
        )

    @staticmethod
    def _prior_run(status, *, iteration: int = 0, result=None, pre_handoff_aborted: bool = False):
        from _run_routing_factories import _make_run

        from shared.contracts.dto.run import RunType

        return _make_run(
            id="eng-abc",
            type=RunType.ENGINEERING,
            status=status,
            result=result,
            run_metadata={
                "triggered_by": "dispatcher",
                "iteration": iteration,
                "pre_handoff_aborted": pre_handoff_aborted,
            },
        )

    async def test_next_tick_after_transition_failure_only_transitions(
        self, api_client, redis_client
    ):
        """This task's own live run means: finish the transition, dispatch nothing.

        The message went out on an earlier tick, so the tick counts it — the work
        is real and running — but it creates no second attempt for it.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.RECOVER_OWN_ATTEMPT
        )

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    async def test_a_live_foreign_attempt_is_adopted_without_being_counted(
        self, api_client, redis_client
    ):
        """Somebody else's attempt still holds the branch: adopt it, dispatch nothing.

        The task leaves todo so nothing tries to dispatch it again, and the tick
        does not count it: this tick put no work behind it.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.ADOPT_LIVE_ATTEMPT
        )

        assert await dispatch_todo_tasks(api_client, redis_client) == 0

        redis_client.publish_message.assert_not_called()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")

    async def test_next_tick_replays_completed_run_onto_task(self, api_client, redis_client):
        """Worker finished before the tick: replay its outcome, don't redispatch."""
        from shared.contracts.dto.run import RunStatus
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.REPLAY_FINISHED_RUN
        )
        api_client.get_run.return_value = self._prior_run(
            RunStatus.COMPLETED,
            result={"engineering_status": "done", "commit_sha": "abc123"},
        )

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        api_client.get_run.assert_awaited_once_with("eng-abc")
        redis_client.publish_message.assert_not_called()
        assert [c[0][1] for c in api_client.transition_task.call_args_list] == [
            "in_dev",
            "in_ci",
            "testing",
            "done",
        ]

    async def test_next_tick_replays_failed_run_onto_task(self, api_client, redis_client):
        """A finished-and-failed run leaves the task failed, for the supervisor to retry."""
        from shared.contracts.dto.run import RunStatus
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.REPLAY_FINISHED_RUN
        )
        api_client.get_run.return_value = self._prior_run(
            RunStatus.FAILED, result={"engineering_status": "failed"}
        )

        await dispatch_todo_tasks(api_client, redis_client)

        redis_client.publish_message.assert_not_called()
        assert [c[0][1] for c in api_client.transition_task.call_args_list] == [
            "in_dev",
            "failed",
        ]

    async def test_next_tick_replays_gave_up_run_onto_task(self, api_client, redis_client):
        """A worker that gave up sends the task to human review, not back to the queue."""
        from shared.contracts.dto.run import RunStatus
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task()]
        api_client.get_task_events.return_value = []
        api_client.admit_engineering_dispatch.return_value = _repair(
            EngineeringDispatchRepair.REPLAY_FINISHED_RUN
        )
        api_client.get_run.return_value = self._prior_run(
            RunStatus.FAILED, result={"engineering_status": "gave_up"}
        )

        await dispatch_todo_tasks(api_client, redis_client)

        assert [c[0][1] for c in api_client.transition_task.call_args_list] == [
            "in_dev",
            "waiting_human_review",
        ]

    async def test_a_retried_task_the_point_admits_is_dispatched_normally(
        self, api_client, redis_client
    ):
        """A retry is an ordinary admitted dispatch on this side of the seam.

        Whether the task's own bumped `current_iteration` may hide a live run is
        decided inside the admission point, and pinned there:
        services/api/tests/service/test_engineering_dispatch_admission.py.
        """
        from src.tasks.task_dispatcher import dispatch_todo_tasks

        api_client.get_tasks_by_status.return_value = [self._todo_task(current_iteration=1)]
        api_client.get_task_events.return_value = []

        dispatched = await dispatch_todo_tasks(api_client, redis_client)

        assert dispatched == 1
        redis_client.publish_message.assert_called_once()
        api_client.transition_task.assert_called_once_with("task-1", "in_dev", "dispatcher")


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

    @pytest.mark.asyncio
    async def test_pr_already_merged_transitions_to_pr_review(self, api_client, redis_client):
        """PR already merged (QA fix cycle) -> transition to pr_review for poller.

        When a PR is already merged (e.g. QA fix task pushed commits and PR
        auto-merged while story was in_progress), complete_stories transitions
        to pr_review so poll_merged_prs() can detect the merge and trigger deploy.
        """
        from unittest.mock import patch

        from src.tasks.task_dispatcher import complete_stories

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, title="Add weather API")
        ]
        api_client.get_tasks_by_story.return_value = [
            _task(id="task-1", status="done", story_id="story-1", project_id=PROJ_ID),
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )

        mock_github = AsyncMock()
        # PR already merged (e.g., QA fix cycle)
        mock_github.create_pull_request.return_value = {
            "number": 42,
            "node_id": "PR_abc",
            "merged_at": "2026-03-19T01:00:00Z",
        }

        with patch("src.tasks.story_completion.GitHubAppClient", return_value=mock_github):
            result = await complete_stories(api_client, redis_client)

        # Must transition to pr_review so poller picks up the merge
        api_client.transition_story.assert_called_once_with("story-1", "pr_review")
        assert result == 1


class TestSuperviseFailedTasks:
    """Supervisor retries failed tasks or escalates to WHR."""

    @pytest.mark.asyncio
    async def test_retries_failed_task_with_iterations_left(self, api_client, redis_client):
        """Failed task with retries left → retry (backlog → todo)."""
        from src.tasks.supervisor import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                story_id="story-1",
                current_iteration=0,
                max_iterations=3,
                status="failed",
                project_id=PROJ_ID,
            )
        ]

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 1
        assert result["escalated"] == 0

    @pytest.mark.asyncio
    async def test_escalates_failed_task_retries_exhausted(self, api_client, redis_client):
        """Failed task with retries exhausted → waiting_human_review."""
        from src.tasks.supervisor import supervise_failed_tasks

        api_client.get_tasks_by_status.return_value = [
            _task(
                id="task-1",
                story_id="story-1",
                current_iteration=3,
                max_iterations=3,
                status="failed",
                project_id=PROJ_ID,
            )
        ]
        api_client.transition_task.return_value = {}
        api_client.transition_story.return_value = {}

        result = await supervise_failed_tasks(api_client, redis_client)

        assert result["retried"] == 0
        assert result["escalated"] == 1
        # Task should be transitioned to WHR
        api_client.transition_task.assert_called_once_with(
            "task-1", "waiting_human_review", "supervisor"
        )


class TestPollMergedPRs:
    """Poll GitHub for merged PRs on stories in pr_review."""

    @pytest.mark.asyncio
    async def test_triggers_create_deploy_for_first_story(self, api_client, redis_client):
        """First story merge -> action='create'."""
        from unittest.mock import patch

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=42)
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )
        # No completed stories — first deploy
        api_client.get_stories_by_project.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=42),
        ]
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.get_pull_request.return_value = {
            "number": 42,
            "merged_at": "2026-03-16T12:00:00Z",
            "head": {"sha": "a" * 40},
        }

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=mock_github):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        api_client.transition_story.assert_called_once_with("story-1", "deploy")
        redis_client.publish_message.assert_called_once()

        deploy_msg = redis_client.publish_message.call_args[0][1]
        assert deploy_msg.project_id == PROJ_ID
        assert deploy_msg.story_id == "story-1"
        assert deploy_msg.action == "create"

    @pytest.mark.asyncio
    async def test_triggers_feature_deploy_when_previous_story_completed(
        self, api_client, redis_client
    ):
        """Project with a completed story -> action='feature'."""
        from unittest.mock import patch

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-2", project_id=PROJ_ID, status="pr_review", pr_number=43)
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )
        # Has a previously completed story
        api_client.get_stories_by_project.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="completed"),
            _story(id="story-2", project_id=PROJ_ID, status="pr_review", pr_number=43),
        ]
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.get_pull_request.return_value = {
            "number": 43,
            "merged_at": "2026-03-16T13:00:00Z",
            "head": {"sha": "d" * 40},
        }

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=mock_github):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        deploy_msg = redis_client.publish_message.call_args[0][1]
        assert deploy_msg.action == "feature"

    @pytest.mark.asyncio
    async def test_no_action_when_pr_not_merged(self, api_client, redis_client):
        """Story in pr_review with open (not merged) PR -> no action."""
        from unittest.mock import patch

        from src.tasks.task_dispatcher import poll_merged_prs

        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=42)
        ]
        api_client.get_primary_repository.return_value = _repo(
            id="repo-1",
            git_url="https://github.com/my-org/weather-bot",
            project_id=PROJ_ID,
        )

        mock_github = AsyncMock()
        mock_github.get_pull_request.return_value = {
            "number": 42,
            "merged_at": None,
            "head": {"sha": "a" * 40},
        }

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

        proj2_id = "00000000-0000-0000-0000-000000000002"
        api_client.get_stories_by_status.return_value = [
            _story(id="story-1", project_id=PROJ_ID, status="pr_review", pr_number=9),
            _story(id="story-2", project_id=proj2_id, status="pr_review", pr_number=10),
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
                project_id=proj2_id,
            ),
        ]
        # First story for this project → action=create
        api_client.get_stories_by_project.return_value = [
            _story(id="story-2", project_id=proj2_id, status="pr_review", pr_number=10),
        ]
        api_client.transition_story.return_value = {}

        mock_github = AsyncMock()
        mock_github.get_pull_request.side_effect = [
            Exception("GitHub API error"),
            {"number": 10, "merged_at": "2026-03-16T12:00:00Z", "head": {"sha": "d" * 40}},
        ]

        with patch("src.tasks.pr_poller.GitHubAppClient", return_value=mock_github):
            result = await poll_merged_prs(api_client, redis_client)

        assert result == 1
        api_client.transition_story.assert_called_once_with("story-2", "deploy")
