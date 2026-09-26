"""A failed planning attempt is a visible, bounded-retried, operator-rerunnable story state.

Canary 2, story-92b433c8: the only paid channel answered 402, the architect
released its attempt and returned, and the story stayed ``in_progress`` with the
failed attempt's three unadmitted ``todo`` tasks. A re-queue then skipped it as
"already decomposed", so recovery took a direct SQL ``UPDATE``.

These tests run the real consumer against an API fake that keeps one story row
and decides every reported outcome with the same contract functions the API
router applies (`failed_record` / `planned_record`), so the class paths below
are the decisions production makes, not a double's call list.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.contracts.dto.llm_channel import LLMChannel
from shared.contracts.dto.product_brief import ProductBriefPlanningAttemptOutcome
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import StoryFailureCode
from shared.contracts.dto.story_planning import (
    StoryPlanningOutcome,
    StoryPlanningState,
    failed_record,
    planned_record,
)
from shared.contracts.queues.architect import ArchitectMessage
from src.llm.chain import _usage
from src.llm.errors import (
    ChannelAttempt,
    ChannelFailureClass,
    LLMChannelsExhausted,
    retry_cannot_fix,
)
from tests.unit.factories import (
    make_admission,
    make_planning_attempt,
    make_product_brief,
    make_project,
    make_story,
    make_task,
)

MAX_RETRIES = 3
STORY_ID = "story-92b433c8"


def _exhausted(*pairs: tuple[LLMChannel, ChannelFailureClass]) -> LLMChannelsExhausted:
    return LLMChannelsExhausted(
        "architect",
        [ChannelAttempt(channel, failure, f"{failure.value} reason") for channel, failure in pairs],
    )


PAYMENT_ON_THE_ONLY_PAID_CHANNEL = ((LLMChannel.OPENROUTER, ChannelFailureClass.PAYMENT_REQUIRED),)
EVERY_CHANNEL_UNFIXABLE = (
    (LLMChannel.CODEX, ChannelFailureClass.QUOTA_EXHAUSTED),
    (LLMChannel.CLAUDE, ChannelFailureClass.MISSING_CREDENTIAL),
    (LLMChannel.OPENROUTER, ChannelFailureClass.PAYMENT_REQUIRED),
)
ONE_CHANNEL_RATE_LIMITED = (
    (LLMChannel.CODEX, ChannelFailureClass.RATE_LIMITED),
    (LLMChannel.CLAUDE, ChannelFailureClass.MISSING_CREDENTIAL),
    (LLMChannel.OPENROUTER, ChannelFailureClass.PAYMENT_REQUIRED),
)


class _PlanningAPI:
    """One story row, its tasks and its brief, as the architect reaches them."""

    def __init__(self, *, status=StoryStatus.CREATED, tasks=(), brief=None, **story) -> None:
        self.story = make_story(id=STORY_ID, status=status, **story)
        self.tasks = list(tasks)
        self.brief = brief
        self.reports: list = []
        self.claims = 0
        self.transitions: list[str] = []

    # --- reads before planning ---

    async def get_story(self, story_id):
        return self.story

    async def get_project(self, project_id, **_kwargs):
        return make_project(config={})

    async def get_agent_config(self, agent_id):
        return {"id": "architect", "llm_channels": [{"channel": "openrouter"}]}

    async def get_tasks_by_story(self, story_id):
        return list(self.tasks)

    async def transition_story(self, story_id, action):
        assert action == "start"
        self.transitions.append(action)
        self.story = self.story.model_copy(update={"status": StoryStatus.IN_PROGRESS})
        return self.story

    # --- the Product Brief boundary ---

    async def get_product_brief_by_story(self, story_id):
        return self.brief

    async def claim_planning_attempt(self, brief_id):
        """The API's claim: a new attempt id, and the superseded plan voided."""
        self.claims += 1
        superseded = self.brief.planning_attempt_id
        new_id = f"plan-{self.claims}"
        self.tasks = [
            task.model_copy(update={"status": "cancelled"})
            if task.planning_attempt_id == superseded and not task.dispatch_admitted
            else task
            for task in self.tasks
        ]
        self.brief = self.brief.model_copy(
            update={
                "planning_attempt_id": new_id,
                "planning_attempt_active": True,
                "planning_attempt_heartbeat_at": datetime.now(UTC),
            }
        )
        return make_planning_attempt(story_id=STORY_ID, planning_attempt_id=new_id)

    async def heartbeat_planning_attempt(self, brief_id, planning_attempt_id):
        return make_planning_attempt(planning_attempt_id=planning_attempt_id)

    async def finish_planning_attempt(self, brief_id, planning_attempt_id):
        self.brief = self.brief.model_copy(update={"planning_attempt_active": False})
        return make_planning_attempt(
            outcome=ProductBriefPlanningAttemptOutcome.RELEASED,
            planning_attempt_id=planning_attempt_id,
        )

    async def admit_product_brief_coverage(self, brief_id, planning_attempt_id):
        self.brief = self.brief.model_copy(update={"coverage_admitted_at": datetime.now(UTC)})
        return make_admission(story_id=STORY_ID)

    async def list_requirement_coverage(self, brief_id):
        return []

    # --- the outcome, decided as `POST /stories/{id}/planning-outcome` decides it ---

    async def record_planning_outcome(self, story_id, report):
        self.reports.append(report)
        now = datetime.now(UTC)
        if report.outcome is StoryPlanningOutcome.SUCCEEDED:
            planning = planned_record(report, now)
        else:
            planning = failed_record(self.story.planning, report, max_retries=MAX_RETRIES, now=now)
        update = {"planning": planning}
        if planning.state is StoryPlanningState.PARKED:
            update |= {
                "status": StoryStatus.WAITING_HUMAN_REVIEW,
                "quarantine_reason": report.failure.model_dump(mode="json"),
            }
        self.story = self.story.model_copy(update=update)
        return planning


def _graph(*outcomes):
    """A graph whose runs raise or answer in order; an answer names its channel.

    A callable outcome is run as the planning itself, and may raise.
    """
    graph = MagicMock()
    runs = list(outcomes)

    async def ainvoke(*_args, **_kwargs):
        outcome = runs.pop(0)
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        _usage.get().answered.append(outcome)
        return {"messages": [{"role": "assistant", "content": "planned"}]}

    graph.ainvoke = AsyncMock(side_effect=ainvoke)
    return graph


def _job(**overrides) -> dict:
    return ArchitectMessage(
        story_id=STORY_ID, project_id="proj-123", telegram_chat_id="user-1", **overrides
    ).model_dump(mode="json")


async def _run(api: _PlanningAPI, graph, job: dict | None = None) -> dict:
    with (
        patch("src.consumers.architect.api_client", api),
        patch("src.consumers.architect.create_architect_graph", return_value=graph),
        patch("src.consumers.architect.get_settings") as settings,
    ):
        settings.return_value = MagicMock(
            architect_llm_api_key="test-key",
            architect_llm_model="test-model",
            architect_llm_base_url="http://test",
        )
        from src.consumers.architect import process_architect_job

        return await process_architect_job(job or _job(), AsyncMock())


# --- which failures a retry can clear ------------------------------------------


@pytest.mark.parametrize(
    ("error", "unfixable"),
    [
        (_exhausted(*PAYMENT_ON_THE_ONLY_PAID_CHANNEL), True),
        (_exhausted(*EVERY_CHANNEL_UNFIXABLE), True),
        (_exhausted((LLMChannel.CODEX, ChannelFailureClass.UNAUTHORIZED)), True),
        (_exhausted((LLMChannel.CODEX, ChannelFailureClass.FORBIDDEN)), True),
        (_exhausted(*ONE_CHANNEL_RATE_LIMITED), False),
        (_exhausted((LLMChannel.CODEX, ChannelFailureClass.SERVER_ERROR)), False),
        (_exhausted((LLMChannel.CODEX, ChannelFailureClass.TIMEOUT)), False),
        (_exhausted((LLMChannel.CODEX, ChannelFailureClass.UNREACHABLE)), False),
        (_exhausted((LLMChannel.CODEX, ChannelFailureClass.NONZERO_EXIT)), False),
        (_exhausted((LLMChannel.CODEX, ChannelFailureClass.INVALID_OUTPUT)), False),
        (RuntimeError("the API answered 503"), False),
    ],
)
def test_only_a_chain_that_failed_everywhere_for_good_is_unretriable(error, unfixable):
    assert retry_cannot_fix(error) is unfixable


# --- each class path ------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_is_retried_and_the_retry_plans_the_story():
    api = _PlanningAPI()
    graph = _graph(_exhausted(*ONE_CHANNEL_RATE_LIMITED), LLMChannel.CLAUDE)

    failed = await _run(api, graph)

    assert failed["status"] == "failed"
    assert failed["planning"] == "retrying"
    planning = api.story.planning
    assert api.story.status is StoryStatus.IN_PROGRESS
    assert planning.state is StoryPlanningState.RETRYING
    assert planning.failed_attempts == 1
    assert planning.next_attempt_at > datetime.now(UTC)
    assert planning.last_failure.code is StoryFailureCode.PLANNING_FAILED
    assert planning.last_failure.source == "architect"
    assert planning.last_failure.detail == (
        "LLMChannelsExhausted: every LLM channel failed: codex:rate_limited, "
        "claude:missing_credential, openrouter:payment_required"
    )

    # The supervisor's re-queue: the same story, still in_progress, planned again.
    succeeded = await _run(api, graph)

    assert succeeded["status"] == "success"
    assert api.story.planning.state is StoryPlanningState.PLANNED
    assert api.story.planning.failed_attempts == 0
    # Which channel planned this story is on the story, not only in a log line.
    assert api.story.planning.channels == ["claude"]
    assert api.story.quarantine_reason is None


@pytest.mark.asyncio
async def test_transient_failures_past_the_bound_park_the_story():
    api = _PlanningAPI()
    failures = [RuntimeError(f"LLM call timed out ({n})") for n in range(MAX_RETRIES + 1)]
    graph = _graph(*failures)

    for attempt in range(1, MAX_RETRIES + 1):
        result = await _run(api, graph)
        assert result["planning"] == "retrying"
        assert api.story.planning.failed_attempts == attempt
        assert api.story.status is StoryStatus.IN_PROGRESS

    parked = await _run(api, graph)

    assert parked["planning"] == "parked"
    assert api.story.status is StoryStatus.WAITING_HUMAN_REVIEW
    assert api.story.planning.state is StoryPlanningState.PARKED
    assert api.story.planning.failed_attempts == MAX_RETRIES + 1
    stop = api.story.quarantine_reason
    assert stop["code"] == "planning_failed"
    assert stop["detail"].startswith("[LLM channels answered: none; failed: none] RuntimeError:")
    assert all(report.retriable for report in api.reports)


@pytest.mark.asyncio
async def test_a_failure_no_retry_can_clear_parks_the_story_at_once():
    api = _PlanningAPI()

    result = await _run(api, _graph(_exhausted(*EVERY_CHANNEL_UNFIXABLE)))

    assert result["planning"] == "parked"
    assert api.story.status is StoryStatus.WAITING_HUMAN_REVIEW
    assert api.story.planning.failed_attempts == 1
    assert api.story.planning.next_attempt_at is None
    assert [report.retriable for report in api.reports] == [False]
    assert "codex:quota_exhausted" in api.story.quarantine_reason["detail"]


@pytest.mark.asyncio
async def test_an_unrunnable_channel_chain_parks_the_story_instead_of_leaving_it_started():
    api = _PlanningAPI()
    with patch("src.consumers.architect.unconfigured_channel_env", return_value=["X_API_KEY"]):
        result = await _run(api, _graph())

    assert result == {
        "status": "failed",
        "error": "X_API_KEY not set",
        "planning": "parked",
        "_live_work_settled": True,
    }
    assert api.story.status is StoryStatus.WAITING_HUMAN_REVIEW
    assert "X_API_KEY not set" in api.story.quarantine_reason["detail"]


@pytest.mark.asyncio
async def test_an_unrecorded_failure_is_replayed_not_swallowed():
    """The story must not stay in_progress with a released attempt and nothing scheduled."""
    from src.consumers.architect import PlanningFailureUnrecordedError

    api = _PlanningAPI()
    api.record_planning_outcome = AsyncMock(side_effect=RuntimeError("API down"))

    with pytest.raises(PlanningFailureUnrecordedError):
        await _run(api, _graph(RuntimeError("LLM timeout")))


# --- what counts as "already decomposed" ---------------------------------------


def _brief(**overrides):
    return make_product_brief(story_id=STORY_ID, **overrides)


def _leftover(task_id: str, **overrides):
    """An unadmitted task of the failed attempt `plan-old`."""
    base = {
        "id": task_id,
        "story_id": STORY_ID,
        "status": "todo",
        "planning_attempt_id": "plan-old",
        "dispatch_admitted": False,
    }
    return make_task(**(base | overrides))


@pytest.mark.asyncio
async def test_regression_story_92b433c8_a_402_then_a_requeue_reaches_the_claim_and_plans():
    """402 on the only paid channel, three unadmitted todo tasks left, then a re-queue."""
    api = _PlanningAPI(brief=_brief())

    def plan_three_tasks_then_402():
        attempt = api.brief.planning_attempt_id
        api.tasks = [_leftover(f"task-{n}", planning_attempt_id=attempt) for n in (1, 2, 3)]
        return _exhausted(*PAYMENT_ON_THE_ONLY_PAID_CHANNEL)

    graph = _graph(plan_three_tasks_then_402, LLMChannel.CODEX)

    first = await _run(api, graph)

    # The failure is the story's state, with its reason — not in_progress, no errors.
    assert first["planning"] == "parked"
    assert api.story.status is StoryStatus.WAITING_HUMAN_REVIEW
    assert api.story.quarantine_reason["detail"] == (
        "LLMChannelsExhausted: every LLM channel failed: openrouter:payment_required"
    )
    assert api.brief.planning_attempt_active is False
    assert [(t.status, t.dispatch_admitted) for t in api.tasks] == [("todo", False)] * 3

    # The operator's retry-planning (`POST /stories/{id}/retry-planning`): failure
    # and retry count cleared, story back in_progress, one architect message.
    api.story = api.story.model_copy(
        update={"status": StoryStatus.IN_PROGRESS, "quarantine_reason": None, "planning": None}
    )

    second = await _run(api, graph)

    assert second["status"] == "success"
    assert api.claims == 2
    assert graph.ainvoke.await_count == 2
    # The claim voided the failed attempt's leftovers, as the manual SQL did.
    assert [task.status for task in api.tasks] == ["cancelled"] * 3
    assert api.story.planning.state is StoryPlanningState.PLANNED
    assert api.story.planning.channels == ["codex"]


@pytest.mark.asyncio
async def test_a_requeue_of_a_story_left_with_a_failed_attempts_leftovers_is_not_skipped():
    api = _PlanningAPI(
        brief=_brief(planning_attempt_id="plan-old", planning_attempt_active=False),
        tasks=[_leftover("task-1"), _leftover("task-2"), _leftover("task-3")],
        status=StoryStatus.IN_PROGRESS,
    )

    result = await _run(api, _graph(LLMChannel.CODEX))

    assert result["status"] == "success"
    # The claim voided what the failed attempt left behind, as the manual SQL did.
    assert [task.status for task in api.tasks] == ["cancelled"] * 3


@pytest.mark.asyncio
async def test_cancelled_tasks_are_not_a_plan_without_a_brief():
    api = _PlanningAPI(
        tasks=[make_task(story_id=STORY_ID, status="cancelled")], status=StoryStatus.IN_PROGRESS
    )

    result = await _run(api, _graph(LLMChannel.CODEX))

    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_a_story_without_a_brief_and_with_tasks_is_skipped_as_today():
    api = _PlanningAPI(tasks=[make_task(story_id=STORY_ID)], status=StoryStatus.IN_PROGRESS)
    graph = _graph()

    result = await _run(api, graph)

    assert result == {
        "status": "skipped",
        "reason": "already decomposed",
        "_live_work_settled": True,
    }
    graph.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_admitted_tasks_are_a_plan():
    api = _PlanningAPI(
        brief=_brief(coverage_admitted_at=datetime.now(UTC), planning_attempt_id="plan-old"),
        tasks=[_leftover("task-1", dispatch_admitted=True)],
        status=StoryStatus.IN_PROGRESS,
    )
    graph = _graph()

    result = await _run(api, graph)

    assert result["reason"] == "already decomposed"
    assert api.claims == 0
    graph.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_a_live_attempts_tasks_are_a_plan_being_made():
    api = _PlanningAPI(
        brief=_brief(
            planning_attempt_id="plan-old",
            planning_attempt_active=True,
            planning_attempt_heartbeat_at=datetime.now(UTC),
        ),
        tasks=[_leftover("task-1")],
        status=StoryStatus.IN_PROGRESS,
    )

    result = await _run(api, _graph())

    assert result["reason"] == "already decomposed"
    assert api.claims == 0


@pytest.mark.asyncio
async def test_a_dead_attempts_tasks_are_not_a_plan():
    api = _PlanningAPI(
        brief=_brief(
            planning_attempt_id="plan-old",
            planning_attempt_active=True,
            planning_attempt_heartbeat_at=datetime.now(UTC) - timedelta(hours=1),
        ),
        tasks=[_leftover("task-1")],
        status=StoryStatus.IN_PROGRESS,
    )

    result = await _run(api, _graph(LLMChannel.CODEX))

    assert result["status"] == "success"
    assert api.claims == 1


@pytest.mark.asyncio
async def test_a_reopened_retry_does_not_mistake_the_closed_cycle_for_a_plan():
    """After retry-planning a reopened story is in_progress with only last cycle's done tasks."""
    reopened_at = datetime.now(UTC)
    api = _PlanningAPI(
        tasks=[
            make_task(story_id=STORY_ID, status="done", created_at=reopened_at - timedelta(days=1))
        ],
        status=StoryStatus.IN_PROGRESS,
        reopened_at=reopened_at,
        user_report="still broken",
    )
    graph = _graph(LLMChannel.CODEX)

    result = await _run(api, graph, _job(is_reopen=True, user_report="still broken"))

    assert result["status"] == "success"
    assert "REOPEN" in graph.ainvoke.call_args.args[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_a_reopen_whose_planning_failed_records_the_reopen_for_its_retry():
    api = _PlanningAPI(status=StoryStatus.REOPENED, user_report="still broken")

    await _run(
        api,
        _graph(RuntimeError("LLM timeout")),
        _job(is_reopen=True, user_report="still broken"),
    )

    assert api.story.planning.reopen is True
    assert api.story.planning.state is StoryPlanningState.RETRYING
