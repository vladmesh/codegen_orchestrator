"""Unit tests for architect consumer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.contracts.dto.product_brief import (
    PLANNING_ATTEMPT_HEARTBEAT_TIMEOUT_SECONDS,
    InitialSetting,
    ProductBriefAdmissionOutcome,
    ProductBriefContent,
    ProductBriefPlanningAttemptOutcome,
    RequirementCoverageRead,
    SettingScope,
)
from shared.contracts.dto.project import ProjectStatus
from shared.contracts.dto.story import StoryStatus
from shared.contracts.dto.story_failure import StoryFailureCode
from shared.contracts.dto.story_planning import StoryPlanning, StoryPlanningState
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


_OPENROUTER_ONLY_CONFIG = {"id": "architect", "llm_channels": [{"channel": "openrouter"}]}


def _recorded_planning(state: StoryPlanningState = StoryPlanningState.RETRYING) -> StoryPlanning:
    """What `POST /stories/{id}/planning-outcome` answers with, reduced to the record."""
    return StoryPlanning(state=state, failed_attempts=1, recorded_at="2026-09-26T00:00:00Z")


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
        mock_api.list_requirement_coverage = AsyncMock(return_value=[])
        # The Architect's stored channel chain: openrouter alone, the chain these
        # tests were written against (`ARCHITECT_LLM_*` is then required).
        mock_api.get_agent_config = AsyncMock(return_value=_OPENROUTER_ONLY_CONFIG)
        # Every planning attempt reports its outcome on the story.
        mock_api.record_planning_outcome = AsyncMock(return_value=_recorded_planning())
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


class TestScaffoldFailureStopsTheStory:
    """Incident 2026-09-24: a dead scaffold left story-3990e41c in_progress with no tasks."""

    @pytest.fixture
    def valid_job_data(self):
        return ArchitectMessage(
            story_id="story-3990e41c", project_id="proj-123", telegram_chat_id="user-1"
        ).model_dump(mode="json")

    @pytest.mark.asyncio
    async def test_a_recorded_scaffold_error_fails_the_story_at_once(
        self, valid_job_data, _mock_api_get_project
    ):
        mock_api = _mock_api_get_project
        mock_api.get_project = AsyncMock(
            return_value=make_project(
                status=ProjectStatus.DRAFT,
                config={"scaffold_error": "Git init/fetch failed: Repository not found."},
            )
        )
        mock_api.stop_story = AsyncMock()
        sleep = AsyncMock()

        with patch("src.consumers.architect.asyncio.sleep", sleep):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, AsyncMock())

        assert result["status"] == "failed"
        assert result["_live_work_settled"] is True
        sleep.assert_not_awaited()
        (call,) = mock_api.stop_story.await_args_list
        story_id, action, failure = call.args
        assert (story_id, action, call.kwargs["actor"]) == ("story-3990e41c", "fail", "architect")
        assert failure.code is StoryFailureCode.SCAFFOLD_FAILED
        assert "Repository not found" in failure.detail

    @pytest.mark.asyncio
    async def test_an_error_recorded_during_the_wait_ends_it(
        self, valid_job_data, _mock_api_get_project
    ):
        mock_api = _mock_api_get_project
        mock_api.get_project = AsyncMock(
            side_effect=[
                make_project(status=ProjectStatus.DRAFT, config={}),
                make_project(status=ProjectStatus.DRAFT, config={"scaffold_error": "clone"}),
            ]
        )
        mock_api.stop_story = AsyncMock()

        with patch("src.consumers.architect.asyncio.sleep", new_callable=AsyncMock) as sleep:
            from src.consumers.architect import process_architect_job

            await process_architect_job(valid_job_data, AsyncMock())

        assert sleep.await_count == 1
        assert mock_api.stop_story.await_args.args[1] == "fail"

    @pytest.mark.asyncio
    async def test_a_timeout_without_a_recorded_error_parks_the_story_for_a_person(
        self, valid_job_data, _mock_api_get_project
    ):
        mock_api = _mock_api_get_project
        mock_api.get_project = AsyncMock(
            return_value=make_project(status=ProjectStatus.DRAFT, config={})
        )
        mock_api.stop_story = AsyncMock()

        with patch("src.consumers.architect.asyncio.sleep", new_callable=AsyncMock):
            from src.consumers.architect import SCAFFOLD_WAIT_MAX, process_architect_job

            result = await process_architect_job(valid_job_data, AsyncMock())

        assert result["error"] == "scaffold did not complete in time"
        story_id, action, failure = mock_api.stop_story.await_args.args
        assert action == "human-review"
        assert failure.code is StoryFailureCode.SCAFFOLD_TIMEOUT
        assert f"after {SCAFFOLD_WAIT_MAX} seconds" in failure.detail

    @pytest.mark.asyncio
    async def test_a_refused_stop_is_logged_and_leaves_the_job_unsettled(
        self, valid_job_data, _mock_api_get_project
    ):
        mock_api = _mock_api_get_project
        mock_api.get_project = AsyncMock(
            return_value=make_project(status=ProjectStatus.DRAFT, config={"scaffold_error": "x"})
        )
        mock_api.stop_story = AsyncMock(side_effect=RuntimeError("422 already failed"))

        from src.consumers.architect import process_architect_job

        result = await process_architect_job(valid_job_data, AsyncMock())

        assert result["status"] == "failed"
        assert result["_live_work_settled"] is False


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
        assert state["initial_settings"] == []

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
        self.planning_reports: list = []

    # --- the story/project reads the consumer does before planning ---

    async def get_agent_config(self, agent_id):
        return _OPENROUTER_ONLY_CONFIG

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

    async def record_planning_outcome(self, story_id, report):
        self.planning_reports.append(report)
        return _recorded_planning()

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

    async def list_requirement_coverage(self, brief_id):
        return [
            RequirementCoverageRead(
                id=n,
                brief_id=brief_id,
                requirement_id=requirement_id,
                planning_attempt_id=attempt,
                task_id=task_id,
                returned_reason=reason,
            )
            for n, (requirement_id, (attempt, task_id, reason)) in enumerate(
                self.coverage.items(), start=1
            )
        ]

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


class TestProductBriefUsageExamples:
    """How the user confirmed each requirement is used reaches the plan in their words."""

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

    async def _instructions(self, api, mock_redis, valid_job_data, brief) -> str:
        api.get_product_brief_by_story = AsyncMock(return_value=brief)
        api.claim_planning_attempt = AsyncMock(return_value=make_planning_attempt())
        api.admit_product_brief_coverage = AsyncMock(return_value=make_admission())
        graph = _graph_returning()
        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)
        assert result["status"] == "success"
        return graph.ainvoke.call_args[0][0]["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_examples_grouped_by_requirement_limitations_and_internal_requirements(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        brief = make_product_brief(
            content=ProductBriefContent(
                summary="Бот личных финансов",
                language="ru",
                must_requirements=[
                    {"id": "expense-text", "text": "Записывает расход из текста"},
                    {"id": "income", "text": "Записывает доход"},
                    {"id": "backup", "text": "Ночная резервная копия", "user_facing": False},
                ],
                # Shown to the user out of requirement order; planned in it.
                usage_examples=[
                    {
                        "requirement_id": "income",
                        "user_sends": "/income 80000 зарплата",
                        "product_answers": "Записал доход 80 000 ₽",
                    },
                    {
                        "requirement_id": "expense-text",
                        "user_sends": "текст «кофе 250»",
                        "product_answers": "Записал расход 250 ₽",
                    },
                    {
                        "requirement_id": "expense-text",
                        "user_sends": "текст «такси 600»",
                        "product_answers": "Записал расход 600 ₽",
                    },
                ],
                limitations=["Чеки распознаются бесплатным способом и могут читаться с ошибками."],
            )
        )

        instructions = await self._instructions(
            _mock_api_get_project, mock_redis, valid_job_data, brief
        )

        assert "user's language: ru" in instructions
        assert (
            "[expense-text]\n"
            "  - the user sends: текст «кофе 250»\n"
            "    the product answers: Записал расход 250 ₽\n"
            "  - the user sends: текст «такси 600»\n"
            "    the product answers: Записал расход 600 ₽\n"
            "[income]\n"
            "  - the user sends: /income 80000 зарплата\n"
            "    the product answers: Записал доход 80 000 ₽"
        ) in instructions
        assert "- backup: Ночная резервная копия (not user-facing" in instructions
        assert "- expense-text: Записывает расход из текста\n" in instructions
        assert (
            "Limitations and trade-offs the user confirmed:\n"
            "- Чеки распознаются бесплатным способом и могут читаться с ошибками."
        ) in instructions
        # The three rules, next to the examples they apply to.
        assert "(requirement <id>)" in instructions
        assert "returned_reason" in instructions and "undefined input" in instructions
        assert "asks the user back" in instructions
        # The coverage boundary is untouched.
        assert "record_requirement_coverage" in instructions

    @pytest.mark.asyncio
    async def test_a_legacy_brief_without_examples_still_gets_valid_instructions(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        instructions = await self._instructions(
            _mock_api_get_project, mock_redis, valid_job_data, make_product_brief()
        )

        assert "- req-1: It must sign users in\n- req-2: It must list cities\n" in instructions
        assert "record_requirement_coverage" in instructions
        assert "user's language" not in instructions
        assert "the user sends:" not in instructions
        assert "Limitations and trade-offs" not in instructions
        assert "not user-facing" not in instructions


class TestProductBriefInitialSettings:
    """The typed settings the user confirmed reach the plan as data.

    They are not disposed of one by one — that is what a must-requirement is.
    What the architect owes them is the declaration that makes them writable in
    the generated product at all, so they travel on the state and are named in
    the instructions.
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

    @staticmethod
    def _brief_with_settings(settings):
        return make_product_brief(
            content=ProductBriefContent(
                summary="A reminder bot",
                must_requirements=[
                    {"id": "req-1", "text": "It must sign users in"},
                    {"id": "req-2", "text": "It must list cities"},
                ],
                initial_settings=settings,
            )
        )

    async def _run(self, api, mock_redis, valid_job_data, settings):
        api.get_product_brief_by_story = AsyncMock(return_value=self._brief_with_settings(settings))
        api.claim_planning_attempt = AsyncMock(return_value=make_planning_attempt())
        api.admit_product_brief_coverage = AsyncMock(return_value=make_admission())
        graph = _graph_returning()
        with patch("src.consumers.architect.create_architect_graph", return_value=graph):
            from src.consumers.architect import process_architect_job

            result = await process_architect_job(valid_job_data, mock_redis)
        assert result["status"] == "success"
        return graph.ainvoke.call_args[0][0]

    @pytest.mark.asyncio
    async def test_confirmed_settings_reach_the_state_and_the_prompt(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        state = await self._run(
            _mock_api_get_project,
            mock_redis,
            valid_job_data,
            [
                InitialSetting(key="reminders.default_hour", value=9),
                InitialSetting(
                    key="reminders.locale", scope=SettingScope.USER, subject_id=7, value="ru"
                ),
            ],
        )

        assert [s.key for s in state["initial_settings"]] == [
            "reminders.default_hour",
            "reminders.locale",
        ]
        # Same run, same instructions: the coverage boundary is untouched.
        user_msg = state["messages"][0]["content"]
        assert "record_requirement_coverage" in user_msg
        assert "req-1" in user_msg and "req-2" in user_msg
        # Each key, its scope, its subject and the confirmed value, verbatim.
        assert "reminders.default_hour" in user_msg
        assert "= 9" in user_msg
        assert "reminders.locale" in user_msg
        assert "subject_id: 7" in user_msg
        assert '= "ru"' in user_msg
        # And what the architect owes them: the manifest declaration.
        assert "manifest.yaml" in user_msg
        assert "settings_schema" in user_msg
        assert "settings_schemas.py" in user_msg
        assert "POST /settings/set" in user_msg
        assert "POST /settings/get" in user_msg

    @pytest.mark.asyncio
    async def test_package_owned_setting_names_the_declared_seed_path(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        state = await self._run(
            _mock_api_get_project,
            mock_redis,
            valid_job_data,
            [InitialSetting(key="reminders.reminder_owner_ref", value="owner-e2e")],
        )

        user_msg = state["messages"][0]["content"]
        assert "reminders.reminder_owner_ref" in user_msg
        assert "package-owned" in user_msg
        assert "declares its setting seed" in user_msg
        assert "DB trigger" in user_msg
        assert "startup polling" in user_msg
        assert "product-owned seed code" in user_msg
        assert "duplicate" in user_msg and "service" in user_msg

    @pytest.mark.asyncio
    async def test_a_brief_that_confirmed_no_settings_says_nothing_about_them(
        self, mock_redis, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        state = await self._run(_mock_api_get_project, mock_redis, valid_job_data, [])

        assert state["initial_settings"] == []
        user_msg = state["messages"][0]["content"]
        assert "record_requirement_coverage" in user_msg
        assert "settings_schema" not in user_msg


class _FakeRedis:
    """`po:input` and the notice marker, in memory; the first `fail_publishes` publishes raise."""

    def __init__(self, fail_publishes: int = 0):
        self.published: list[tuple[str, dict]] = []
        self.keys: dict[str, str] = {}
        self.fail_publishes = fail_publishes
        self.redis = self

    async def publish_flat(self, stream, fields):
        if self.fail_publishes:
            self.fail_publishes -= 1
            raise ConnectionError("po:input unavailable")
        self.published.append((stream, fields))

    async def exists(self, key):
        return int(key in self.keys)

    async def set(self, key, value):
        self.keys[key] = value


_RETURNED_REASON = "Undefined input: which cities, and does the user type them or pick them?"


def _coverage(*rows: tuple[str, str, str | None, str | None]) -> list[RequirementCoverageRead]:
    return [
        RequirementCoverageRead(
            id=n,
            brief_id="brief-1",
            requirement_id=requirement_id,
            planning_attempt_id=attempt,
            task_id=task_id,
            returned_reason=reason,
        )
        for n, (requirement_id, attempt, task_id, reason) in enumerate(rows, start=1)
    ]


_ONE_RETURNED = _coverage(
    # A superseded attempt's return is not this plan's.
    ("req-1", "plan-old", None, "stale reason of a voided attempt"),
    ("req-1", "plan-1", "task-1", None),
    ("req-2", "plan-1", None, _RETURNED_REASON),
)
_NONE_RETURNED = _coverage(("req-1", "plan-1", "task-1", None), ("req-2", "plan-1", "task-1", None))


def _worded_brief(**overrides):
    return make_product_brief(
        content=ProductBriefContent(
            summary="A product",
            language="ru",
            must_requirements=[
                {"id": "req-1", "text": "It must sign users in"},
                {"id": "req-2", "text": "It must list cities", "user_wording": "покажи города"},
            ],
        ),
        **overrides,
    )


def _returned_events(redis: _FakeRedis) -> list[dict]:
    return [
        fields for _, fields in redis.published if fields["event"] == "story_requirements_returned"
    ]


class TestReturnedRequirementsNotice:
    """A requirement the admitted plan returned is told to the owner, once, never silently lost."""

    @pytest.fixture
    def valid_job_data(self):
        return ArchitectMessage(
            story_id="story-abc",
            project_id="proj-123",
            telegram_chat_id="user-1",
        ).model_dump(mode="json")

    def _admitting(self, api, coverage):
        api.get_product_brief_by_story = AsyncMock(return_value=_worded_brief())
        api.claim_planning_attempt = AsyncMock(return_value=make_planning_attempt())
        api.admit_product_brief_coverage = AsyncMock(return_value=make_admission())
        api.list_requirement_coverage = AsyncMock(return_value=coverage)

    def _replaying(self, api, *, story_status="created", tasks=()):
        api.get_story = AsyncMock(return_value=make_story(id="story-abc", status=story_status))
        api.get_tasks_by_story = AsyncMock(return_value=list(tasks))
        api.get_product_brief_by_story = AsyncMock(
            return_value=_worded_brief(
                coverage_admitted_at=make_product_brief().confirmed_at,
                planning_attempt_id="plan-1",
            )
        )
        api.claim_planning_attempt = AsyncMock(
            return_value=make_planning_attempt(
                outcome=ProductBriefPlanningAttemptOutcome.ALREADY_ADMITTED,
                planning_attempt_id=None,
            )
        )

    async def _run(self, job, redis):
        with patch(
            "src.consumers.architect.create_architect_graph", return_value=_graph_returning()
        ):
            from src.consumers.architect import process_architect_job

            return await process_architect_job(job, redis)

    @pytest.mark.asyncio
    async def test_one_returned_requirement_is_one_event_to_the_owner(
        self, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        from shared.queues import PO_INPUT_QUEUE

        self._admitting(_mock_api_get_project, _ONE_RETURNED)
        redis = _FakeRedis()

        result = await self._run(valid_job_data, redis)

        assert result["status"] == "success"
        assert len(redis.published) == 1
        stream, event = redis.published[0]
        assert stream == PO_INPUT_QUEUE
        assert event["event"] == "story_requirements_returned"
        assert event["type"] == "system_event"
        assert event["telegram_chat_id"] == "user-1"
        assert event["story_id"] == "story-abc"
        assert event["project_id"] == "proj-123"
        text = event["text"]
        assert "- req-2: It must list cities" in text
        assert "the user's words: покажи города" in text
        assert f"reason: {_RETURNED_REASON}" in text
        assert "User's language: ru" in text
        assert "will NOT be built" in text and "The rest of the story is being built" in text
        # Only what this plan returned: the covered requirement and the voided
        # attempt's return are not in it.
        assert "req-1" not in text and "stale reason" not in text

    @pytest.mark.asyncio
    async def test_a_requirement_qa_cannot_check_is_returned_like_any_other(
        self, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        from shared.contracts.dto.product_brief import NOT_AUTOMATICALLY_VERIFIABLE_PREFIX

        reason = (
            f"{NOT_AUTOMATICALLY_VERIFIABLE_PREFIX} QA would need to read the email the "
            "product sends to the user"
        )
        self._admitting(
            _mock_api_get_project,
            _coverage(("req-1", "plan-1", "task-1", None), ("req-2", "plan-1", None, reason)),
        )
        redis = _FakeRedis()

        result = await self._run(valid_job_data, redis)

        assert result["status"] == "success"
        [event] = _returned_events(redis)
        assert event["story_id"] == "story-abc"
        assert "- req-2: It must list cities" in event["text"]
        assert f"reason: {reason}" in event["text"]
        assert "req-1" not in event["text"]

    @pytest.mark.asyncio
    async def test_nothing_returned_publishes_nothing(
        self, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        self._admitting(_mock_api_get_project, _NONE_RETURNED)
        redis = _FakeRedis()

        result = await self._run(valid_job_data, redis)

        assert result["status"] == "success"
        assert redis.published == []

    @pytest.mark.asyncio
    async def test_a_failed_publish_leaves_the_job_unacknowledged_and_the_replay_publishes(
        self, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        from src.consumers.architect import ReturnedRequirementsNoticeError

        api = _mock_api_get_project
        self._admitting(api, _ONE_RETURNED)
        redis = _FakeRedis(fail_publishes=1)

        # Raised out of the job, not turned into a failed (and acknowledged) result.
        with pytest.raises(ReturnedRequirementsNoticeError, match="po:input unavailable"):
            await self._run(valid_job_data, redis)
        assert redis.published == []
        api.admit_product_brief_coverage.assert_awaited_once()
        # The admitted plan is not given back as if planning had failed.
        api.finish_planning_attempt.assert_not_called()

        # The reclaimed entry replays through the ALREADY_ADMITTED claim.
        self._replaying(api)
        result = await self._run(valid_job_data, redis)

        assert result["status"] == "success"
        assert len(_returned_events(redis)) == 1
        assert "reason: " + _RETURNED_REASON in _returned_events(redis)[0]["text"]
        api.admit_product_brief_coverage.assert_awaited_once()

        # A delivered notice is not published again by a later run on the same plan.
        await self._run(valid_job_data, redis)
        assert len(_returned_events(redis)) == 1

    @pytest.mark.asyncio
    async def test_the_replay_of_an_already_decomposed_story_publishes_the_owed_notice(
        self, valid_job_data, _mock_api_get_project, _llm_configured
    ):
        api = _mock_api_get_project
        self._replaying(api, story_status=StoryStatus.IN_PROGRESS, tasks=[make_task()])
        api.list_requirement_coverage = AsyncMock(return_value=_ONE_RETURNED)
        redis = _FakeRedis()

        result = await self._run(valid_job_data, redis)

        assert result["status"] == "skipped"
        assert len(_returned_events(redis)) == 1
        api.claim_planning_attempt.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_recipient_is_alerted_not_retried(
        self, _mock_api_get_project, _llm_configured
    ):
        self._admitting(_mock_api_get_project, _ONE_RETURNED)
        job = ArchitectMessage(story_id="story-abc", project_id="proj-123").model_dump(mode="json")
        redis = _FakeRedis()

        with patch("src.consumers.architect.notify_admins_best_effort") as alert:
            result = await self._run(job, redis)

        assert result["status"] == "success"
        assert redis.published == []
        alert.assert_awaited_once()
        assert "no Telegram recipient" in alert.call_args.args[0]
        assert "req-2" in alert.call_args.args[0]
