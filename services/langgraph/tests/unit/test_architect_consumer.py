"""Unit tests for architect consumer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.contracts.dto.product_brief import (
    PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS,
    ProductBriefAdmissionOutcome,
    ProductBriefPlanningAttemptOutcome,
    RequirementCoverageRead,
)
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.queues.architect import ArchitectMessage
from tests.unit.factories import (
    make_admission,
    make_planning_attempt,
    make_product_brief,
    make_project,
    make_story,
    make_task,
)

# Default project response (ACTIVE = scaffold done, no waiting)
_ACTIVE_PROJECT = make_project(status=ProjectStatus.ACTIVE, config={})

# Default story response (CREATED = ready for architect decomposition)
_CREATED_STORY = make_story(id="story-abc", status="created")


@pytest.fixture(autouse=True)
def _mock_api_get_project():
    """All tests get a pre-scaffolded (ACTIVE) project and CREATED story by default."""
    with patch("src.consumers.architect.api_client") as mock_api:
        mock_api.get_project = AsyncMock(return_value=_ACTIVE_PROJECT)
        mock_api.get_story = AsyncMock(return_value=_CREATED_STORY)
        # Preserve other methods as AsyncMock so tests can override
        mock_api.get_tasks_by_story = AsyncMock(return_value=[])
        mock_api.transition_story = AsyncMock()
        # The default story is not backed by a Product Brief, so the default
        # run is the one the architect has always done: no claim, no coverage,
        # no admission.
        mock_api.get_product_brief_by_story = AsyncMock(return_value=None)
        mock_api.claim_planning_attempt = AsyncMock()
        mock_api.heartbeat_planning_attempt = AsyncMock()
        mock_api.finish_planning_attempt = AsyncMock()
        mock_api.admit_product_brief_coverage = AsyncMock()
        yield mock_api


class TestProcessArchitectJob:
    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()

    @pytest.fixture
    def valid_job_data(self):
        msg = ArchitectMessage(
            story_id="story-abc",
            project_id="proj-123",
            telegram_chat_id="user-1",
        )
        return msg.model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_invalid_message_reaches_terminal_consumer_boundary(self, mock_redis):
        from src.consumers._base import TerminalMessageValidationError
        from src.consumers.architect import process_architect_job

        with pytest.raises(TerminalMessageValidationError):
            await process_architect_job({"bad": "data"}, mock_redis)

    @pytest.mark.asyncio
    async def test_skips_deploying_story(self, mock_redis, valid_job_data, _mock_api_get_project):
        """Architect skips stories that are already deploying.

        NOTE: COMPLETED/ARCHIVED/FAILED are now caught by the centralized
        staleness guard in _base.py and never reach process_architect_job.
        """
        _mock_api_get_project.get_story = AsyncMock(
            return_value=make_story(id="story-abc", status=StoryStatus.DEPLOYING)
        )
        from src.consumers.architect import process_architect_job

        result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "skipped"
        assert StoryStatus.DEPLOYING in result["reason"]

    @pytest.mark.asyncio
    async def test_skips_when_story_not_found(
        self, mock_redis, valid_job_data, _mock_api_get_project
    ):
        """Architect skips when story no longer exists (404)."""
        _mock_api_get_project.get_story = AsyncMock(side_effect=Exception("404 Not Found"))
        from src.consumers.architect import process_architect_job

        result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "skipped"
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_fails_without_llm_config(self, mock_redis, valid_job_data):
        with patch("src.consumers.architect.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                architect_llm_api_key=None,
                architect_llm_model=None,
                architect_llm_base_url=None,
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        assert "not set" in result["error"]

    @pytest.mark.asyncio
    async def test_invokes_graph_on_valid_message(self, mock_redis, valid_job_data):
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        mock_graph.ainvoke.assert_called_once()

        # Verify state passed to graph
        call_args = mock_graph.ainvoke.call_args[0][0]
        assert call_args["story_id"] == "story-abc"
        assert call_args["project_id"] == "proj-123"
        assert len(call_args["messages"]) == 1

    @pytest.mark.asyncio
    async def test_reopen_message_includes_user_report(self, mock_redis, _mock_api_get_project):
        """Reopen messages include user_report in the initial state."""
        _mock_api_get_project.get_story = AsyncMock(
            return_value=make_story(id="story-reopen", status=StoryStatus.REOPENED)
        )
        reopen_data = ArchitectMessage(
            story_id="story-reopen",
            project_id="proj-123",
            telegram_chat_id="user-1",
            is_reopen=True,
            user_report="Images broken on mobile",
        ).model_dump(mode="json")

        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(reopen_data, mock_redis)

        assert result["status"] == "success"
        call_args = mock_graph.ainvoke.call_args[0][0]
        user_msg = call_args["messages"][0]["content"]
        assert "REOPEN" in user_msg
        assert "Images broken on mobile" in user_msg
        assert "get_tasks_by_story" in user_msg
        # Verify story was transitioned to in_progress after architect finished
        _mock_api_get_project.transition_story.assert_called_with("story-reopen", "start")

    @pytest.mark.asyncio
    async def test_normal_message_no_reopen_context(self, mock_redis, valid_job_data):
        """Normal messages use standard decomposition prompt."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        call_args = mock_graph.ainvoke.call_args[0][0]
        user_msg = call_args["messages"][0]["content"]
        assert "REOPEN" not in user_msg
        assert "Decompose story" in user_msg

    @pytest.mark.asyncio
    async def test_handles_graph_error(self, mock_redis, valid_job_data):
        mock_graph = AsyncMock()
        mock_graph.ainvoke.side_effect = RuntimeError("LLM timeout")

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        assert "LLM timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_waits_for_scaffold_then_proceeds(
        self, mock_redis, valid_job_data, _mock_api_get_project
    ):
        """Architect waits when project is DRAFT, proceeds when it becomes ACTIVE."""
        mock_api = _mock_api_get_project
        # First call: DRAFT, second call: ACTIVE
        mock_api.get_project = AsyncMock(
            side_effect=[
                make_project(status=ProjectStatus.DRAFT, config={}),
                make_project(status=ProjectStatus.ACTIVE, config={}),
            ]
        )

        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
            patch("src.consumers.architect.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="test-key",
                architect_llm_model="test-model",
                architect_llm_base_url="http://test",
            )
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        assert mock_api.get_project.call_count == 2


class TestProcessArchitectJobIntegration:
    """Integration-style test: full flow with mocked graph + mocked API."""

    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()

    @pytest.fixture
    def valid_job_data(self):
        msg = ArchitectMessage(
            story_id="story-int",
            project_id="proj-int",
            telegram_chat_id="user-1",
        )
        return msg.model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_full_flow_creates_tasks(self, mock_redis, valid_job_data, _mock_api_get_project):
        """Graph creates architect tasks — no CI task appended."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke.return_value = {"messages": [{"role": "assistant", "content": "done"}]}

        with (
            patch("src.consumers.architect.get_settings") as mock_settings,
            patch("src.consumers.architect.create_architect_graph", return_value=mock_graph),
        ):
            mock_settings.return_value = MagicMock(
                architect_llm_api_key="key",
                architect_llm_model="model",
                architect_llm_base_url="http://test",
            )

            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"


@pytest.fixture
def _llm_configured():
    """The LLM settings every planning run needs before it reaches the graph."""
    with patch("src.consumers.architect.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            architect_llm_api_key="test-key",
            architect_llm_model="test-model",
            architect_llm_base_url="http://test",
        )
        yield mock_settings


def _graph_returning(messages=None):
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": messages or [{"role": "assistant", "content": "ok"}]}
    return graph


class TestProductBriefPlanning:
    """The consumer as the producer of the released Product Brief boundary."""

    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()

    @pytest.fixture
    def valid_job_data(self):
        return ArchitectMessage(
            story_id="story-abc",
            project_id="proj-123",
            telegram_chat_id="user-1",
        ).model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_story_without_a_brief_touches_no_boundary(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        """No brief means exactly today's run: no claim, no coverage, no admission."""
        graph = _graph_returning()
        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        _mock_api_get_project.claim_planning_attempt.assert_not_called()
        _mock_api_get_project.admit_product_brief_coverage.assert_not_called()
        _mock_api_get_project.heartbeat_planning_attempt.assert_not_called()
        state = graph.ainvoke.call_args[0][0]
        assert state["product_brief_id"] is None
        assert state["planning_attempt_id"] is None
        assert state["must_requirements"] == []

    @pytest.mark.asyncio
    async def test_claimed_plan_runs_under_the_attempt_and_admits_once(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        api = _mock_api_get_project
        api.get_product_brief_by_story = AsyncMock(return_value=make_product_brief())
        api.claim_planning_attempt = AsyncMock(return_value=make_planning_attempt())
        api.admit_product_brief_coverage = AsyncMock(
            return_value=make_admission(released_task_ids=["task-1", "task-2"])
        )
        graph = _graph_returning()

        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        state = graph.ainvoke.call_args[0][0]
        assert state["product_brief_id"] == "brief-1"
        assert state["planning_attempt_id"] == "plan-1"
        assert [r.id for r in state["must_requirements"]] == ["req-1", "req-2"]
        # The requirement ids the model must dispose of reach the model.
        user_msg = state["messages"][0]["content"]
        assert "req-1" in user_msg and "req-2" in user_msg
        assert "record_requirement_coverage" in user_msg
        api.claim_planning_attempt.assert_awaited_once_with("brief-1")
        api.admit_product_brief_coverage.assert_awaited_once_with("brief-1", "plan-1")

    @pytest.mark.asyncio
    async def test_rival_owner_plans_nothing(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        """`in_progress` names the rival attempt and this run stops there."""
        api = _mock_api_get_project
        api.get_product_brief_by_story = AsyncMock(return_value=make_product_brief())
        api.claim_planning_attempt = AsyncMock(
            return_value=make_planning_attempt(
                outcome=ProductBriefPlanningAttemptOutcome.IN_PROGRESS,
                planning_attempt_id="plan-rival",
            )
        )
        graph = _graph_returning()

        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "skipped"
        assert result["planning_attempt_id"] == "plan-rival"
        graph.ainvoke.assert_not_called()
        api.admit_product_brief_coverage.assert_not_called()
        api.heartbeat_planning_attempt.assert_not_called()

    @pytest.mark.asyncio
    async def test_already_admitted_plan_is_not_admitted_again(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        """A released plan is ordinary work now: no attempt on the run, no second admit."""
        api = _mock_api_get_project
        api.get_product_brief_by_story = AsyncMock(
            return_value=make_product_brief(coverage_admitted_at=make_product_brief().confirmed_at)
        )
        api.claim_planning_attempt = AsyncMock(
            return_value=make_planning_attempt(
                outcome=ProductBriefPlanningAttemptOutcome.ALREADY_ADMITTED,
                planning_attempt_id=None,
            )
        )
        graph = _graph_returning()

        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        api.admit_product_brief_coverage.assert_not_called()
        assert graph.ainvoke.call_args[0][0]["planning_attempt_id"] is None

    @pytest.mark.asyncio
    async def test_unconfirmed_brief_is_not_planned(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        api = _mock_api_get_project
        api.get_product_brief_by_story = AsyncMock(
            return_value=make_product_brief(confirmed_at=None, confirmation_request_id=None)
        )
        graph = _graph_returning()

        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "skipped"
        api.claim_planning_attempt.assert_not_called()
        graph.ainvoke.assert_not_called()

    def test_heartbeat_interval_is_below_the_contract_timeout(self):
        from src.consumers.architect import PLANNING_HEARTBEAT_INTERVAL

        assert 0 < PLANNING_HEARTBEAT_INTERVAL < PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_across_a_slow_run_and_stops_after_it(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        api = _mock_api_get_project
        api.get_product_brief_by_story = AsyncMock(return_value=make_product_brief())
        api.claim_planning_attempt = AsyncMock(return_value=make_planning_attempt())
        api.admit_product_brief_coverage = AsyncMock(return_value=make_admission())

        graph = AsyncMock()

        async def slow_plan(*_args, **_kwargs):
            await asyncio.sleep(0.1)
            return {"messages": []}

        graph.ainvoke.side_effect = slow_plan

        with (
            patch("src.consumers.architect.create_architect_graph", return_value=graph),
            patch("src.consumers.architect.PLANNING_HEARTBEAT_INTERVAL", 0.01),
        ):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        beats = api.heartbeat_planning_attempt.await_count
        assert beats >= 2
        api.heartbeat_planning_attempt.assert_awaited_with("brief-1", "plan-1")
        # Nothing is left beating after the job returned.
        await asyncio.sleep(0.05)
        assert api.heartbeat_planning_attempt.await_count == beats

    @pytest.mark.asyncio
    async def test_heartbeat_stops_and_attempt_is_released_when_the_graph_raises(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        api = _mock_api_get_project
        api.get_product_brief_by_story = AsyncMock(return_value=make_product_brief())
        api.claim_planning_attempt = AsyncMock(return_value=make_planning_attempt())

        graph = AsyncMock()

        async def failing_plan(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            raise RuntimeError("LLM timeout")

        graph.ainvoke.side_effect = failing_plan

        with (
            patch("src.consumers.architect.create_architect_graph", return_value=graph),
            patch("src.consumers.architect.PLANNING_HEARTBEAT_INTERVAL", 0.01),
        ):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "failed"
        assert "LLM timeout" in result["error"]
        beats = api.heartbeat_planning_attempt.await_count
        assert beats >= 1
        await asyncio.sleep(0.05)
        assert api.heartbeat_planning_attempt.await_count == beats
        # The plan is given back, so recovery need not wait out the timeout.
        api.finish_planning_attempt.assert_awaited_once_with("brief-1", "plan-1")
        api.admit_product_brief_coverage.assert_not_called()

    @pytest.mark.asyncio
    async def test_incomplete_admission_releases_nothing_and_says_so(
        self, mock_redis, _mock_api_get_project, _llm_configured
    ):
        """Even when the LLM reported success, an incomplete plan is the result."""
        api = _mock_api_get_project
        api.get_story = AsyncMock(return_value=make_story(id="story-abc", status="reopened"))
        api.get_product_brief_by_story = AsyncMock(return_value=make_product_brief())
        api.claim_planning_attempt = AsyncMock(return_value=make_planning_attempt())
        api.admit_product_brief_coverage = AsyncMock(
            return_value=make_admission(
                outcome=ProductBriefAdmissionOutcome.INCOMPLETE,
                coverage_admitted_at=None,
                released_task_ids=[],
                missing_requirement_ids=["req-2"],
            )
        )
        reopen_data = ArchitectMessage(
            story_id="story-abc",
            project_id="proj-123",
            telegram_chat_id="user-1",
            is_reopen=True,
            user_report="still broken",
        ).model_dump(mode="json")
        graph = _graph_returning([{"role": "assistant", "content": "all done!"}])

        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(reopen_data, mock_redis)

        assert result["status"] == "incomplete"
        assert result["missing_requirement_ids"] == ["req-2"]
        assert "req-2" in result["error"]
        api.admit_product_brief_coverage.assert_awaited_once_with("brief-1", "plan-1")
        # The story is not moved on by this consumer, and nothing is admitted twice.
        api.transition_story.assert_not_called()


class _FakeBriefBoundary:
    """The released Product Brief boundary, in memory, for the rules under test.

    Only the rules this counterfactual turns on are modelled, and each is the
    one the API declares: a task created under an active attempt is created
    *unadmitted* under that attempt, a disposition counts only under the attempt
    that wrote it, and `admit` releases nothing while a must-requirement is
    undisposed. Nothing here is a second admission surface — the consumer under
    test writes `dispatch_admitted` nowhere, and this fake is the only thing
    that ever sets it.
    """

    def __init__(self, brief):
        self.brief = brief
        self.attempt_id: str | None = None
        self.attempt_active = False
        self.coverage: dict[str, tuple[str, str | None, str | None]] = {}
        self.tasks: dict[str, dict] = {}
        self.admit_calls = 0
        self.released: list[str] = []

    # --- the story/project reads the consumer does before planning ---

    async def get_story(self, story_id):
        return make_story(id=story_id, status="created")

    async def get_project(self, project_id, **_kwargs):
        return make_project(status=ProjectStatus.ACTIVE, config={})

    async def get_tasks_by_story(self, story_id):
        return []

    async def transition_story(self, story_id, action):
        return make_story(id=story_id, status="in_progress")

    async def get_primary_repository(self, project_id):
        return None

    # --- the boundary ---

    async def get_product_brief_by_story(self, story_id):
        return self.brief

    async def claim_planning_attempt(self, brief_id):
        self.attempt_id = "plan-live"
        self.attempt_active = True
        return make_planning_attempt(planning_attempt_id=self.attempt_id)

    async def heartbeat_planning_attempt(self, brief_id, planning_attempt_id):
        return make_planning_attempt(planning_attempt_id=planning_attempt_id)

    async def finish_planning_attempt(self, brief_id, planning_attempt_id):
        self.attempt_active = False
        return make_planning_attempt(
            outcome=ProductBriefPlanningAttemptOutcome.RELEASED,
            planning_attempt_id=planning_attempt_id,
        )

    async def create_task(self, task_data):
        task_id = f"task-{len(self.tasks) + 1}"
        attempt = task_data.get("planning_attempt_id")
        # `plan_admission_for_new_task`: unadmitted only under an active attempt.
        admitted = not (self.attempt_active and attempt == self.attempt_id)
        self.tasks[task_id] = {"planning_attempt_id": attempt, "dispatch_admitted": admitted}
        return make_task(
            id=task_id,
            title=task_data["title"],
            story_id=task_data["story_id"],
            planning_attempt_id=attempt,
            dispatch_admitted=admitted,
        )

    async def record_requirement_coverage(self, brief_id, coverage):
        if coverage.planning_attempt_id != self.attempt_id or not self.attempt_active:
            raise httpx.HTTPStatusError(
                "conflict",
                request=httpx.Request("PUT", "http://api/coverage"),
                response=httpx.Response(409, json={"detail": "requires the active attempt"}),
            )
        self.coverage[coverage.requirement_id] = (
            coverage.planning_attempt_id,
            coverage.task_id,
            coverage.returned_reason,
        )
        return RequirementCoverageRead(
            id=len(self.coverage),
            brief_id=brief_id,
            requirement_id=coverage.requirement_id,
            planning_attempt_id=coverage.planning_attempt_id,
            task_id=coverage.task_id,
            returned_reason=coverage.returned_reason,
        )

    async def admit_product_brief_coverage(self, brief_id, planning_attempt_id):
        self.admit_calls += 1
        must = {r.id for r in self.brief.content.must_requirements}
        covered = {
            rid for rid, (attempt, _, _) in self.coverage.items() if attempt == planning_attempt_id
        }
        missing = sorted(must - covered)
        if missing:
            return make_admission(
                outcome=ProductBriefAdmissionOutcome.INCOMPLETE,
                coverage_admitted_at=None,
                released_task_ids=[],
                missing_requirement_ids=missing,
            )
        self.attempt_active = False
        self.released = sorted(
            task_id
            for task_id, task in self.tasks.items()
            if task["planning_attempt_id"] == planning_attempt_id and not task["dispatch_admitted"]
        )
        for task_id in self.released:
            self.tasks[task_id]["dispatch_admitted"] = True
        return make_admission(released_task_ids=list(self.released))


def _planning_graph(dispose: list[str]):
    """A run that plans one task and disposes of exactly the ids in `dispose`.

    It reports success in its messages whatever it disposed of, which is the
    point: the LLM's account of the run is not the evidence.
    """
    from src.agents.architect.tools import create_task, record_requirement_coverage

    graph = MagicMock()

    async def ainvoke(state, config=None):
        created = await create_task.ainvoke(
            {
                "title": "Implement the product",
                "description": "…",
                "type": "feature",
                "acceptance_criteria": "it works",
                "story_id": state["story_id"],
                "project_id": state["project_id"],
                "planning_attempt_id": state["planning_attempt_id"],
            }
        )
        for requirement_id in dispose:
            await record_requirement_coverage.ainvoke(
                {
                    "requirement_id": requirement_id,
                    "task_id": created["id"],
                    "brief_id": state["product_brief_id"],
                    "planning_attempt_id": state["planning_attempt_id"],
                }
            )
        return {"messages": [{"role": "assistant", "content": "All requirements covered!"}]}

    graph.ainvoke = AsyncMock(side_effect=ainvoke)
    return graph


class TestUndisposedRequirementCounterfactual:
    """A plan that leaves one must-requirement undisposed releases nothing.

    Read as a counterfactual, not as a restatement of the invariant: what is
    asserted is what the *boundary* did — the task was created unadmitted under
    the attempt, `admit` was called once, it answered `incomplete`, and no task
    was released. Remove any of this card's wiring and the assertions fail
    rather than pass vacuously: with no attempt on `create_task` the task is
    created admitted and released by nobody's decision; with no coverage call
    the disposed requirement is undisposed too; with no admit call the result is
    a plain success and `admit_calls` is zero.
    """

    @pytest.fixture
    def mock_redis(self):
        return AsyncMock()

    @pytest.fixture
    def valid_job_data(self):
        return ArchitectMessage(
            story_id="story-abc",
            project_id="proj-123",
            telegram_chat_id="user-1",
        ).model_dump(mode="json")

    @pytest.fixture
    def boundary(self):
        fake = _FakeBriefBoundary(make_product_brief())
        with (
            patch("src.consumers.architect.api_client", fake),
            patch("src.agents.architect.tools.api_client", fake),
        ):
            yield fake

    @pytest.mark.asyncio
    async def test_one_undisposed_requirement_ends_incomplete(
        self, mock_redis, valid_job_data, boundary, _llm_configured
    ):
        graph = _planning_graph(dispose=["req-1"])

        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "incomplete"
        assert result["missing_requirement_ids"] == ["req-2"]
        assert boundary.admit_calls == 1
        assert boundary.released == []
        # The task exists, under the attempt, and nothing released it.
        assert list(boundary.tasks.values()) == [
            {"planning_attempt_id": "plan-live", "dispatch_admitted": False}
        ]

    @pytest.mark.asyncio
    async def test_every_requirement_disposed_releases_the_plan(
        self, mock_redis, valid_job_data, boundary, _llm_configured
    ):
        """The same wiring, one disposition more: the plan is released."""
        graph = _planning_graph(dispose=["req-1", "req-2"])

        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)

        assert result["status"] == "success"
        assert boundary.admit_calls == 1
        assert boundary.released == ["task-1"]
        assert boundary.tasks["task-1"]["dispatch_admitted"] is True
