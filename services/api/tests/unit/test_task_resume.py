"""`POST /tasks/{id}/resume` — the operator's one fresh attempt for a parked task.

On 2026-09-17 this route moved a task parked with `current_iteration =
max_iterations` to in_dev and nothing else: no run was created, the supervisor
replayed the last failed run onto it and parked it again within a tick. These
tests hold the route to what it now promises: a fresh iteration no run carries,
a retry budget granted on purpose, the story out of human review with its task,
and a refusal with a reason whenever a second worker could reach the branch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
import uuid

from httpx import ASGITransport, AsyncClient
from internal_caller import INTERNAL_HEADERS
import pytest

from shared.contracts.dto.engineering_dispatch import (
    EngineeringDispatchOutcome,
    EngineeringDispatchRepair,
)
from shared.contracts.dto.engineering_execution import ENGINEERING_INFRASTRUCTURE_KEY
from shared.contracts.dto.run import RunStatus, RunType
from shared.contracts.dto.story import StoryStatus, StoryWaitingOn
from shared.contracts.dto.task import TaskStatus
from shared.models import Run, Task, TaskEvent
from shared.models.story import Story
from src.database import get_async_session
from src.engineering_dispatch_admission import _prior_attempt
from src.main import app
from src.routers import _task_actions
from src.routers._task_actions import RESUME_ACTION, _fresh_iteration

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
TASK_ID = "task-parked"
STORY_ID = "story-parked"


def _task(**overrides) -> Task:
    now = datetime.now(UTC)
    fields = {
        "id": TASK_ID,
        "project_id": PROJECT_ID,
        "type": "feature",
        "title": "Parked after its retries",
        "description": None,
        "plan": None,
        "status": TaskStatus.WAITING_HUMAN_REVIEW.value,
        "priority": 0,
        "acceptance_criteria": None,
        "current_iteration": 3,
        "max_iterations": 3,
        "need_e2e": False,
        "created_by": "architect",
        "story_id": STORY_ID,
        "failure_metadata": None,
        "dispatch_admitted": True,
        "planning_attempt_id": None,
        "created_at": now,
        "updated_at": now,
    }
    fields.update(overrides)
    return Task(**fields)


def _story(status: StoryStatus = StoryStatus.WAITING_HUMAN_REVIEW) -> Story:
    return Story(
        id=STORY_ID,
        project_id=PROJECT_ID,
        title="Parked story",
        status=status.value,
        waiting_on=StoryWaitingOn.HUMAN_REVIEW.value,
    )


def _run(
    status: RunStatus,
    *,
    iteration: int | None,
    task_id: str = TASK_ID,
    run_id: str | None = None,
    **metadata,
) -> Run:
    return Run(
        id=run_id or f"eng-{task_id}-{iteration}-{status.value}",
        project_id=PROJECT_ID,
        task_id=task_id,
        story_id=STORY_ID,
        type=RunType.ENGINEERING.value,
        status=status.value,
        run_metadata={"triggered_by": "dispatcher", "iteration": iteration, **metadata},
        result={"engineering_status": "failed"} if status is RunStatus.FAILED else None,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
    )


def _failed_every_iteration() -> list[Run]:
    return [_run(RunStatus.FAILED, iteration=iteration) for iteration in range(4)]


class _Session:
    """The rows the resume route reads, in the shapes it reads them."""

    def __init__(self, *, siblings: dict[str, str], runs: list[Run]) -> None:
        self.added: list[object] = []
        self.committed = False
        self._siblings = siblings
        self._runs = runs

    async def execute(self, _statement):
        result = MagicMock()
        result.all = MagicMock(return_value=list(self._siblings.items()))
        return result

    async def scalars(self, _statement):
        result = MagicMock()
        result.all = MagicMock(return_value=list(self._runs))
        return result

    def add(self, row: object) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _row: object) -> None:
        return None


@pytest.fixture
def parked(monkeypatch):
    """Wire one task and its story behind the route's locking readers."""

    def wire(
        task: Task,
        story: Story | None,
        *,
        siblings: dict[str, str] | None = None,
        runs: list[Run] | None = None,
    ) -> _Session:
        session = _Session(siblings=siblings or {}, runs=runs or [])

        async def locked_task(task_id, _db):
            assert task_id == task.id
            return task

        async def locked_story(story_id, _db):
            assert story is not None and story_id == story.id
            return story

        monkeypatch.setattr(_task_actions, "get_task_for_update", locked_task)
        monkeypatch.setattr(_task_actions, "_get_story_for_update", locked_story)

        async def override():
            yield session

        app.dependency_overrides[get_async_session] = override
        return session

    yield wire
    app.dependency_overrides.clear()


async def _resume(json: dict | None = None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=INTERNAL_HEADERS
    ) as client:
        return await client.post(
            f"/api/tasks/{TASK_ID}/resume",
            json=json or {"guidance": "the infrastructure is fixed", "actor": "admin"},
        )


@pytest.mark.asyncio
async def test_a_task_parked_after_its_retries_gets_a_fresh_iteration_and_budget(parked):
    task = _task(failure_metadata={"reason": "worker could not be created"})
    story = _story()
    session = parked(task, story, siblings={"task-done": "done"}, runs=_failed_every_iteration())

    response = await _resume()

    assert response.status_code == 200, response.text  # noqa: PLR2004
    assert session.committed
    # Past every run the task has ever had, so nothing of this iteration exists
    # for the dispatcher to replay; the budget is the allowance, granted anew.
    assert (task.status, task.current_iteration, task.max_iterations) == (
        TaskStatus.TODO.value,
        4,
        7,
    )
    assert (story.status, story.waiting_on) == (
        StoryStatus.IN_PROGRESS.value,
        StoryWaitingOn.NONE.value,
    )
    events = [row for row in session.added if isinstance(row, TaskEvent)]
    hops = [(event.from_status, event.to_status) for event in events if event.to_status]
    assert hops == [
        (TaskStatus.WAITING_HUMAN_REVIEW, TaskStatus.BACKLOG),
        (TaskStatus.BACKLOG, TaskStatus.TODO),
    ]
    # The budget is recorded durably, with what it replaced.
    assert events[0].details == {
        "action": RESUME_ACTION,
        "previous_iteration": 3,
        "previous_max_iterations": 3,
        "iteration": 4,
        "max_iterations": 7,
        "retries": 3,
        "previous_failure_metadata": {"reason": "worker could not be created"},
    }
    assert task.failure_metadata is None
    assert events[-1].details == {"action": "guidance", "guidance": "the infrastructure is fixed"}


@pytest.mark.asyncio
async def test_the_operator_names_the_retry_budget(parked):
    task = _task()
    parked(task, _story(), runs=_failed_every_iteration())

    response = await _resume({"guidance": "one shot only", "retries": 0})

    assert response.status_code == 200, response.text  # noqa: PLR2004
    assert (task.current_iteration, task.max_iterations) == (4, 4)


@pytest.mark.asyncio
async def test_a_story_already_back_in_progress_takes_a_resumed_task(parked):
    task, story = _task(), _story(StoryStatus.IN_PROGRESS)
    parked(task, story, siblings={"task-sibling": "todo"})

    response = await _resume()

    assert response.status_code == 200, response.text  # noqa: PLR2004
    assert story.status == StoryStatus.IN_PROGRESS.value
    assert task.current_iteration == 4  # noqa: PLR2004


@pytest.mark.asyncio
async def test_a_run_aborted_before_handoff_holds_no_branch(parked):
    task = _task()
    aborted = _run(RunStatus.QUEUED, iteration=3, pre_handoff_aborted=True)
    parked(task, _story(), runs=[aborted])

    response = await _resume()

    assert response.status_code == 200, response.text  # noqa: PLR2004


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [TaskStatus.IN_DEV, TaskStatus.TODO, TaskStatus.FAILED])
async def test_a_task_that_is_not_parked_is_refused(parked, status):
    task = _task(status=status.value)
    session = parked(task, _story())

    response = await _resume()

    assert response.status_code == 422  # noqa: PLR2004
    assert response.json()["detail"]["reason"] == "task_not_parked"
    assert not session.committed
    assert task.status == status.value


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.QUEUED, RunStatus.RUNNING])
async def test_a_task_with_a_live_run_is_refused(parked, status):
    task, story = _task(), _story()
    session = parked(task, story, runs=[_run(status, iteration=3)])

    response = await _resume()

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["detail"]["reason"] == "live_attempt_in_flight"
    assert not session.committed
    assert (task.status, story.status) == (
        TaskStatus.WAITING_HUMAN_REVIEW.value,
        StoryStatus.WAITING_HUMAN_REVIEW.value,
    )


@pytest.mark.asyncio
async def test_a_sibling_worker_on_the_story_branch_is_refused(parked):
    task = _task()
    sibling_run = _run(RunStatus.RUNNING, iteration=0, task_id="task-sibling")
    session = parked(task, _story(), siblings={"task-sibling": "todo"}, runs=[sibling_run])

    response = await _resume()

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["detail"]["reason"] == "story_busy"
    assert not session.committed


@pytest.mark.asyncio
async def test_a_sibling_in_dev_is_refused(parked):
    task = _task()
    session = parked(task, _story(), siblings={"task-sibling": TaskStatus.IN_DEV.value})

    response = await _resume()

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["detail"]["reason"] == "story_busy"
    assert not session.committed


@pytest.mark.asyncio
async def test_an_infrastructure_park_is_left_to_its_own_retry(parked):
    task = _task(failure_metadata={ENGINEERING_INFRASTRUCTURE_KEY: {"refusal": "x"}})
    session = parked(task, _story())

    response = await _resume()

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["detail"]["reason"] == "infrastructure_parked"
    assert not session.committed


@pytest.mark.asyncio
async def test_a_story_that_ended_is_refused(parked):
    task = _task()
    session = parked(task, _story(StoryStatus.FAILED))

    response = await _resume()

    assert response.status_code == 409  # noqa: PLR2004
    assert response.json()["detail"]["reason"] == "story_not_resumable"
    assert not session.committed


def test_the_replay_rule_still_applies_to_a_task_a_failed_transition_left_in_todo():
    """The case the replay exists for is untouched by the operator's attempt.

    A worker finished iteration 2 while the task's transition out of todo
    failed: admission must still name the replay of that run, not a dispatch.
    """
    task = _task(status=TaskStatus.TODO.value, current_iteration=2)
    finished = _run(RunStatus.FAILED, iteration=2)

    decision = _prior_attempt(task, [finished], "init-run")

    assert decision is not None
    assert decision.outcome is EngineeringDispatchOutcome.REPAIR
    assert decision.repair is EngineeringDispatchRepair.REPLAY_FINISHED_RUN
    assert decision.run_id == finished.id


def test_a_resumed_iteration_has_no_finished_run_to_replay():
    """What tells the two apart: the resumed iteration is one no run carries."""
    runs = [*_failed_every_iteration(), _run(RunStatus.FAILED, iteration=None, run_id="eng-x")]
    task = _task(status=TaskStatus.TODO.value)
    assert _prior_attempt(task, runs, "init-run") is not None

    task.current_iteration = _fresh_iteration(task, runs)

    assert task.current_iteration == 4  # noqa: PLR2004
    assert _prior_attempt(task, runs, "init-run") is None
