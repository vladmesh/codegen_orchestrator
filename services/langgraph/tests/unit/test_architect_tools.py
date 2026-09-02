"""Unit tests for architect tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from shared.contracts.acceptance import parse_scheduled_behaviours
from shared.contracts.dto.product_brief import RequirementCoverageRead
from tests.unit.factories import make_project, make_story, make_task


@pytest.fixture
def mock_api():
    with patch("src.agents.architect.tools.api_client") as api:
        api.get_story = AsyncMock(
            return_value=make_story(id="story-abc", title="Add auth", status="created")
        )
        api.get_project = AsyncMock(
            return_value=make_project(
                title="my-api",
                slug="my-api-0000",
                config={"detailed_spec": "REST"},
            )
        )
        api.get_tasks_by_story = AsyncMock(return_value=[])
        api.create_task = AsyncMock(return_value=make_task(id="task-new", title="New task"))
        api.record_requirement_coverage = AsyncMock(
            return_value=RequirementCoverageRead(
                id=1,
                brief_id="brief-1",
                requirement_id="req-1",
                planning_attempt_id="plan-1",
                task_id="task-new",
            )
        )
        yield api


class TestGetStoryTool:
    @pytest.mark.asyncio
    async def test_returns_story(self, mock_api):
        from src.agents.architect.tools import get_story

        result = await get_story.ainvoke({"story_id": "story-abc"})

        assert result["id"] == "story-abc"
        mock_api.get_story.assert_called_once_with("story-abc")


class TestGetProjectSpecTool:
    @pytest.mark.asyncio
    async def test_returns_project(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        assert result["title"] == "my-api"
        mock_api.get_project.assert_called_once_with("proj-1")

    @pytest.mark.asyncio
    async def test_surfaces_tree_from_config(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = make_project(
            name="my-api",
            config={"tree": ".\n├── src/\n│   └── main.py", "secrets": {"DB": "xxx"}},
            project_spec={"modules": ["backend"]},
        )

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        assert result["tree"] == ".\n├── src/\n│   └── main.py"
        assert result["project_spec"] == {"modules": ["backend"]}
        assert "secrets" not in result.get("config", {})

    @pytest.mark.asyncio
    async def test_handles_missing_tree(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = make_project(
            name="my-api",
            config={},
            project_spec=None,
        )

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        assert result.get("tree") is None
        assert result.get("project_spec") is None

    @pytest.mark.asyncio
    async def test_strips_noisy_config_fields(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = make_project(
            name="my-api",
            config={
                "tree": ".",
                "secrets": {"key": "val"},
                "env_hints": ["hint"],
                "detailed_spec": "important spec",
            },
        )

        result = await get_project_spec.ainvoke({"project_id": "proj-1"})

        config = result.get("config", {})
        assert "secrets" not in config
        assert "env_hints" not in config
        assert config.get("detailed_spec") == "important spec"

    @pytest.mark.asyncio
    async def test_returns_error_when_not_found(self, mock_api):
        from src.agents.architect.tools import get_project_spec

        mock_api.get_project.return_value = None
        result = await get_project_spec.ainvoke({"project_id": "proj-missing"})

        assert "error" in result


class TestGetTasksByStoryTool:
    @pytest.mark.asyncio
    async def test_returns_tasks(self, mock_api):
        from src.agents.architect.tools import get_tasks_by_story

        result = await get_tasks_by_story.ainvoke({"story_id": "story-abc"})

        assert result == []
        mock_api.get_tasks_by_story.assert_called_once_with("story-abc")


class TestCreateTaskTool:
    @pytest.fixture(autouse=True)
    def _reset_chain(self):
        from src.agents.architect.tools import reset_task_chain

        reset_task_chain()
        yield
        reset_task_chain()

    @pytest.mark.asyncio
    async def test_creates_task(self, mock_api):
        from src.agents.architect.tools import create_task

        result = await create_task.ainvoke(
            {
                "title": "Add User model",
                "description": "Create model",
                "type": "feature",
                "acceptance_criteria": "Model exists",
                "story_id": "story-abc",
                "project_id": "proj-1",
            }
        )

        assert result["id"] == "task-new"
        call_args = mock_api.create_task.call_args[0][0]
        assert call_args["title"] == "Add User model"
        assert call_args["status"] == "todo"
        assert call_args["created_by"] == "architect"
        # First task has no dependency
        assert call_args["blocked_by_task_id"] is None

    @pytest.mark.asyncio
    async def test_auto_chains_tasks(self, mock_api):
        """Second task is automatically blocked by the first."""
        from src.agents.architect.tools import create_task

        mock_api.create_task = AsyncMock(
            side_effect=[
                make_task(id="task-001", title="First"),
                make_task(id="task-002", title="Second"),
            ]
        )

        await create_task.ainvoke(
            {
                "title": "First task",
                "description": "Do first thing",
                "type": "feature",
                "acceptance_criteria": "Done",
                "story_id": "story-abc",
                "project_id": "proj-1",
            }
        )
        await create_task.ainvoke(
            {
                "title": "Second task",
                "description": "Do second thing",
                "type": "feature",
                "acceptance_criteria": "Done",
                "story_id": "story-abc",
                "project_id": "proj-1",
            }
        )

        first_call = mock_api.create_task.call_args_list[0][0][0]
        second_call = mock_api.create_task.call_args_list[1][0][0]
        assert first_call["blocked_by_task_id"] is None
        assert second_call["blocked_by_task_id"] == "task-001"


class TestArchitectToolSurface:
    """The architect decomposes; it does not move the story.

    The consumer already transitions the story around this agent's run, so a
    story-transition tool here gave one code path two Story transitions for the
    same story — and hid the API's refusal of the second one behind a 422
    fallback that re-read the story and reported success.
    """

    def test_exposes_no_story_lifecycle_tool(self):
        from src.agents.architect import tools

        assert not hasattr(tools, "transition_story")
        names = {tool.name for tool in tools.get_architect_tools()}
        assert names == {
            "get_story",
            "get_project_spec",
            "get_tasks_by_story",
            "create_task",
            # Added with the Product Brief boundary: one disposition per
            # must-requirement is what releases the plan.
            "record_requirement_coverage",
            "update_acceptance_criteria",
        }

    def test_prompt_does_not_ask_the_agent_to_move_the_story(self):
        from src.prompts.architect import SYSTEM_PROMPT

        assert "transition_story" not in SYSTEM_PROMPT


def _refusal(status_code: int, detail: str) -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        detail,
        request=httpx.Request("PUT", "http://api/coverage"),
        response=httpx.Response(status_code, json={"detail": detail}),
    )


class TestCreateTaskUnderAPlanningAttempt:
    @pytest.fixture(autouse=True)
    def _reset_chain(self):
        from src.agents.architect.tools import reset_task_chain

        reset_task_chain()
        yield
        reset_task_chain()

    @pytest.mark.asyncio
    async def test_carries_the_attempt_when_planning_under_one(self, mock_api):
        """The API creates it unadmitted under this attempt; only admit releases it."""
        from src.agents.architect.tools import create_task

        await create_task.ainvoke(
            {
                "title": "Add User model",
                "description": "Create model",
                "type": "feature",
                "acceptance_criteria": "Model exists",
                "story_id": "story-abc",
                "project_id": "proj-1",
                "planning_attempt_id": "plan-1",
            }
        )

        assert mock_api.create_task.call_args[0][0]["planning_attempt_id"] == "plan-1"

    @pytest.mark.asyncio
    async def test_non_brief_call_shape_is_unchanged(self, mock_api):
        from src.agents.architect.tools import create_task

        await create_task.ainvoke(
            {
                "title": "Add User model",
                "description": "Create model",
                "type": "feature",
                "acceptance_criteria": "Model exists",
                "story_id": "story-abc",
                "project_id": "proj-1",
            }
        )

        assert "planning_attempt_id" not in mock_api.create_task.call_args[0][0]

    def test_the_model_is_not_asked_for_a_planning_attempt(self):
        """Planning identity is injected from state, never a tool argument.

        A model that could name an attempt could plan into a brief it does not
        own, so the id is absent from the schema the model is shown.
        """
        from src.agents.architect.tools import create_task, record_requirement_coverage

        assert "planning_attempt_id" not in create_task.tool_call_schema.model_fields
        for field in ("planning_attempt_id", "brief_id"):
            assert field not in record_requirement_coverage.tool_call_schema.model_fields


class TestRecordRequirementCoverageTool:
    @pytest.mark.asyncio
    async def test_records_a_task_disposition(self, mock_api):
        from src.agents.architect.tools import record_requirement_coverage

        result = await record_requirement_coverage.ainvoke(
            {
                "requirement_id": "req-1",
                "task_id": "task-new",
                "brief_id": "brief-1",
                "planning_attempt_id": "plan-1",
            }
        )

        assert result["requirement_id"] == "req-1"
        brief_id, coverage = mock_api.record_requirement_coverage.call_args[0]
        assert brief_id == "brief-1"
        assert coverage.planning_attempt_id == "plan-1"
        assert coverage.task_id == "task-new"
        assert coverage.returned_reason is None

    @pytest.mark.asyncio
    async def test_records_a_returned_disposition(self, mock_api):
        from src.agents.architect.tools import record_requirement_coverage

        await record_requirement_coverage.ainvoke(
            {
                "requirement_id": "req-2",
                "returned_reason": "needs a payment provider the project has no account with",
                "brief_id": "brief-1",
                "planning_attempt_id": "plan-1",
            }
        )

        _, coverage = mock_api.record_requirement_coverage.call_args[0]
        assert coverage.task_id is None
        assert coverage.returned_reason.startswith("needs a payment provider")

    @pytest.mark.asyncio
    async def test_two_dispositions_at_once_are_refused_readably(self, mock_api):
        from src.agents.architect.tools import record_requirement_coverage

        result = await record_requirement_coverage.ainvoke(
            {
                "requirement_id": "req-1",
                "task_id": "task-new",
                "returned_reason": "also returned",
                "brief_id": "brief-1",
                "planning_attempt_id": "plan-1",
            }
        )

        assert "req-1" in result["error"]
        mock_api.record_requirement_coverage.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_disposition_at_all_is_refused_readably(self, mock_api):
        from src.agents.architect.tools import record_requirement_coverage

        result = await record_requirement_coverage.ainvoke(
            {
                "requirement_id": "req-1",
                "brief_id": "brief-1",
                "planning_attempt_id": "plan-1",
            }
        )

        assert "error" in result
        mock_api.record_requirement_coverage.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_refusal_reaches_the_model_in_its_own_words(self, mock_api):
        """The architect's repair depends on which refusal it was, so it is not flattened."""
        from src.agents.architect.tools import record_requirement_coverage

        mock_api.record_requirement_coverage = AsyncMock(
            side_effect=_refusal(422, "unknown Product Brief must-requirement")
        )

        result = await record_requirement_coverage.ainvoke(
            {
                "requirement_id": "req-typo",
                "task_id": "task-new",
                "brief_id": "brief-1",
                "planning_attempt_id": "plan-1",
            }
        )

        assert "unknown Product Brief must-requirement" in result["error"]

    @pytest.mark.asyncio
    async def test_wrong_attempt_refusal_is_returned_not_raised(self, mock_api):
        from src.agents.architect.tools import record_requirement_coverage

        mock_api.record_requirement_coverage = AsyncMock(
            side_effect=_refusal(
                409, "Product Brief planning requires the currently active attempt"
            )
        )

        result = await record_requirement_coverage.ainvoke(
            {
                "requirement_id": "req-1",
                "task_id": "task-new",
                "brief_id": "brief-1",
                "planning_attempt_id": "plan-superseded",
            }
        )

        assert "currently active attempt" in result["error"]

    @pytest.mark.asyncio
    async def test_a_run_without_a_plan_has_nothing_to_record(self, mock_api):
        from src.agents.architect.tools import record_requirement_coverage

        result = await record_requirement_coverage.ainvoke(
            {"requirement_id": "req-1", "task_id": "task-new"}
        )

        assert "error" in result
        mock_api.record_requirement_coverage.assert_not_called()


class TestUpdateAcceptanceCriteriaContract:
    """The tool teaches the checklist forms the platform itself reads."""

    @staticmethod
    def _contract() -> str:
        from src.agents.architect.tools import update_acceptance_criteria

        return update_acceptance_criteria.description

    def test_still_teaches_the_ordinary_check_forms(self):
        contract = self._contract()

        assert "- GET /health returns 200" in contract
        assert '- POST /api/cities with {"name": "Moscow"} returns 201' in contract

    def test_teaches_the_fire_job_form_and_its_rules(self):
        contract = self._contract()

        assert "- FIRE JOB daily_digest THEN" in contract
        assert "jobs_schema" in contract
        assert "character for character" in contract
        assert "a capability rather than a sample" in contract
        assert "there is a Russian item this week" in contract
        assert "from those confirmed values, not from the story prose" in contract

    def test_every_worked_criterion_line_is_read_by_the_released_parser(self):
        """One pattern: the contract's examples round-trip through the parser itself."""
        worked = [
            line.strip()
            for line in self._contract().splitlines()
            if line.strip().startswith("- FIRE JOB")
        ]

        assert len(worked) == 2
        parsed = [parse_scheduled_behaviours(line) for line in worked]
        assert [len(one) for one in parsed] == [1, 1]
        assert {one[0].name for one in parsed} == {"daily_digest"}
        assert parsed[0][0].arguments == {}
        assert parsed[1][0].arguments == {"languages": ["ru", "en"]}
        assert parsed[1][0].observable == "a digest per configured language"

    def test_an_ordinary_checklist_the_contract_teaches_declares_no_behaviour(self):
        """A story with no scheduled behaviour keeps exactly today's criteria."""
        ordinary = (
            "- GET /health returns 200\n"
            '- POST /api/cities with {"name": "Moscow"} returns 201\n'
            "- Telegram: /start responds with welcome message\n"
        )

        assert parse_scheduled_behaviours(ordinary) == []
