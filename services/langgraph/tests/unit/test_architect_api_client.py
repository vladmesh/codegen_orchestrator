"""Unit tests for LanggraphAPIClient architect methods (story/task)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import httpx
import pytest

from shared.contracts.dto.product_brief import (
    ProductBriefAdmissionOutcome,
    ProductBriefPlanningAttemptOutcome,
    RequirementCoverageCreate,
)
from shared.contracts.dto.story import WAITING_ON_BY_STATUS, StoryStatus


@pytest.fixture
def mock_httpx_client():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.is_closed = False
    return client


@pytest.fixture
def api_client(mock_httpx_client):
    with patch("src.clients.api.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(api_base_url="http://api:8000")
        from src.clients.api import LanggraphAPIClient

        c = LanggraphAPIClient()
        c._client = mock_httpx_client
        return c


_NOW = datetime.now(UTC).isoformat()
_UUID = str(uuid.uuid4())


def _ok_response(data):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = data
    return resp


def _story_dict(**overrides):
    base = {
        "id": "story-abc",
        "project_id": _UUID,
        "title": "Add auth",
        "type": "product",
        "status": "created",
        "priority": 0,
        "created_by": "system",
        "created_at": _NOW,
    }
    base.update(overrides)
    # The API always returns `waiting_on`, and it follows from the status.
    base.setdefault("waiting_on", WAITING_ON_BY_STATUS[StoryStatus(base["status"])].value)
    return base


def _task_dict(**overrides):
    base = {
        "id": "task-1",
        "project_id": _UUID,
        "type": "feature",
        "title": "Test task",
        "status": "todo",
        "priority": 0,
        "current_iteration": 1,
        "max_iterations": 3,
        "created_by": "system",
        # The API always returns it, so a task response without it is not one.
        "dispatch_admitted": True,
        "created_at": _NOW,
    }
    base.update(overrides)
    return base


class TestGetStory:
    @pytest.mark.asyncio
    async def test_returns_story_dto(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(_story_dict())

        result = await api_client.get_story("story-abc")

        assert result.id == "story-abc"
        assert result.title == "Add auth"
        call_args = mock_httpx_client.request.call_args
        assert "/api/stories/story-abc" in str(call_args)


class TestGetTasksByStory:
    @pytest.mark.asyncio
    async def test_returns_task_list(self, api_client, mock_httpx_client):
        tasks = [_task_dict(id="task-1"), _task_dict(id="task-2")]
        mock_httpx_client.request.return_value = _ok_response(tasks)

        result = await api_client.get_tasks_by_story("story-abc")

        assert len(result) == 2
        assert result[0].id == "task-1"
        assert result[1].id == "task-2"
        call_args = mock_httpx_client.request.call_args
        assert "story_id" in str(call_args)


class TestCreateTask:
    @pytest.mark.asyncio
    async def test_creates_and_returns_task(self, api_client, mock_httpx_client):
        created = _task_dict(id="task-new", title="New task")
        mock_httpx_client.request.return_value = _ok_response(created)

        task_data = {
            "title": "New task",
            "description": "Do something",
            "project_id": _UUID,
            "story_id": "story-abc",
        }
        result = await api_client.create_task(task_data)

        assert result.id == "task-new"
        assert result.title == "New task"
        call_args = mock_httpx_client.request.call_args
        assert call_args[0][0] == "POST"
        assert "/api/tasks/" in str(call_args)


class TestTransitionStory:
    @pytest.mark.asyncio
    async def test_transitions_story(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(_story_dict(status="in_progress"))

        result = await api_client.transition_story("story-abc", "start")

        assert result.status == "in_progress"
        call_args = mock_httpx_client.request.call_args
        assert "story-abc" in str(call_args)
        assert "start" in str(call_args)


def _brief_dict(**overrides):
    base = {
        "id": "brief-1",
        "project_id": _UUID,
        "story_id": "story-abc",
        "revision": 1,
        "title": "Test brief",
        "content": {
            "summary": "A product",
            "must_requirements": [{"id": "req-1", "text": "It must sign users in"}],
        },
        "confirmed_at": _NOW,
        "confirmation_request_id": "confirm-1",
        "coverage_admitted_at": None,
        "planning_attempt_id": None,
        "planning_attempt_active": False,
        "planning_attempt_heartbeat_at": None,
    }
    base.update(overrides)
    return base


def _not_found_response():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 404
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found", request=httpx.Request("GET", "http://api/x"), response=resp
    )
    return resp


class TestProductBriefBoundaryClient:
    """The client half of the released boundary — typed answers, not status codes."""

    @pytest.mark.asyncio
    async def test_brief_by_story_returns_the_typed_read(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(_brief_dict())

        brief = await api_client.get_product_brief_by_story("story-abc")

        assert brief is not None
        assert brief.id == "brief-1"
        assert [r.id for r in brief.content.must_requirements] == ["req-1"]
        method, path = mock_httpx_client.request.call_args[0]
        assert (method, path) == ("GET", "/api/product-briefs/by-story/story-abc")

    @pytest.mark.asyncio
    async def test_a_story_with_no_brief_is_none_not_an_error(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _not_found_response()

        assert await api_client.get_product_brief_by_story("story-plain") is None

    @pytest.mark.asyncio
    async def test_claim_returns_the_outcome_and_the_attempt(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(
            {
                "brief_id": "brief-1",
                "story_id": "story-abc",
                "outcome": "claimed",
                "planning_attempt_id": "plan-1",
                "planning_attempt_heartbeat_at": _NOW,
            }
        )

        claim = await api_client.claim_planning_attempt("brief-1")

        assert claim.outcome is ProductBriefPlanningAttemptOutcome.CLAIMED
        assert claim.planning_attempt_id == "plan-1"
        method, path = mock_httpx_client.request.call_args[0]
        assert (method, path) == (
            "POST",
            "/api/product-briefs/brief-1/planning-attempts/claim",
        )

    @pytest.mark.asyncio
    async def test_a_rival_owner_is_a_typed_outcome_not_a_conflict(
        self, api_client, mock_httpx_client
    ):
        mock_httpx_client.request.return_value = _ok_response(
            {
                "brief_id": "brief-1",
                "story_id": "story-abc",
                "outcome": "in_progress",
                "planning_attempt_id": "plan-rival",
                "planning_attempt_heartbeat_at": _NOW,
            }
        )

        claim = await api_client.claim_planning_attempt("brief-1")

        assert claim.outcome is ProductBriefPlanningAttemptOutcome.IN_PROGRESS
        assert claim.planning_attempt_id == "plan-rival"

    @pytest.mark.asyncio
    async def test_heartbeat_and_finish_present_the_attempt(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(
            {
                "brief_id": "brief-1",
                "story_id": "story-abc",
                "outcome": "claimed",
                "planning_attempt_id": "plan-1",
                "planning_attempt_heartbeat_at": _NOW,
            }
        )

        await api_client.heartbeat_planning_attempt("brief-1", "plan-1")
        method, path = mock_httpx_client.request.call_args[0]
        assert (method, path) == (
            "POST",
            "/api/product-briefs/brief-1/planning-attempts/heartbeat",
        )
        assert mock_httpx_client.request.call_args.kwargs["json"] == {
            "planning_attempt_id": "plan-1"
        }

        mock_httpx_client.request.return_value = _ok_response(
            {
                "brief_id": "brief-1",
                "story_id": "story-abc",
                "outcome": "released",
                "planning_attempt_id": "plan-1",
                "planning_attempt_heartbeat_at": _NOW,
            }
        )
        released = await api_client.finish_planning_attempt("brief-1", "plan-1")
        assert released.outcome is ProductBriefPlanningAttemptOutcome.RELEASED
        method, path = mock_httpx_client.request.call_args[0]
        assert (method, path) == (
            "POST",
            "/api/product-briefs/brief-1/planning-attempts/finish",
        )

    @pytest.mark.asyncio
    async def test_coverage_is_written_under_the_attempt(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(
            {
                "id": 1,
                "brief_id": "brief-1",
                "requirement_id": "req-1",
                "planning_attempt_id": "plan-1",
                "task_id": "task-1",
                "returned_reason": None,
            }
        )

        recorded = await api_client.record_requirement_coverage(
            "brief-1",
            RequirementCoverageCreate(
                requirement_id="req-1", planning_attempt_id="plan-1", task_id="task-1"
            ),
        )

        assert recorded.task_id == "task-1"
        method, path = mock_httpx_client.request.call_args[0]
        assert (method, path) == ("PUT", "/api/product-briefs/brief-1/coverage/req-1")

    @pytest.mark.asyncio
    async def test_coverage_listing_is_typed(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(
            [
                {
                    "id": 1,
                    "brief_id": "brief-1",
                    "requirement_id": "req-1",
                    "planning_attempt_id": "plan-1",
                    "task_id": "task-1",
                    "returned_reason": None,
                }
            ]
        )

        rows = await api_client.list_requirement_coverage("brief-1")

        assert [row.requirement_id for row in rows] == ["req-1"]
        method, path = mock_httpx_client.request.call_args[0]
        assert (method, path) == ("GET", "/api/product-briefs/brief-1/coverage")

    @pytest.mark.asyncio
    async def test_admit_returns_the_durable_answer(self, api_client, mock_httpx_client):
        mock_httpx_client.request.return_value = _ok_response(
            {
                "brief_id": "brief-1",
                "story_id": "story-abc",
                "outcome": "incomplete",
                "coverage_admitted_at": None,
                "missing_requirement_ids": ["req-1"],
                "released_task_ids": [],
            }
        )

        admission = await api_client.admit_product_brief_coverage("brief-1", "plan-1")

        assert admission.outcome is ProductBriefAdmissionOutcome.INCOMPLETE
        assert admission.missing_requirement_ids == ["req-1"]
        method, path = mock_httpx_client.request.call_args[0]
        assert (method, path) == ("POST", "/api/product-briefs/brief-1/admit")
        assert mock_httpx_client.request.call_args.kwargs["json"] == {
            "planning_attempt_id": "plan-1"
        }
