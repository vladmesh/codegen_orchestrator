"""The PO sees a failed planning as a problem the platform is handling, never "in progress".

Canary 2: story-92b433c8 could not be planned (an OpenRouter 402), yet it sat
``in_progress`` and the PO read "in progress, no errors". The story now carries
its planning outcome, and these tests hold the PO's tools to it.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from shared.clients.internal_api import InternalAPIClient
from src.agents.po.tools_shared import init_po_clients
from src.agents.po.tools_stories import get_story, list_stories

CAUSE = "LLMChannelsExhausted: every LLM channel failed: openrouter:payment_required"
FAILURE = {
    "reason": "story_failure",
    "code": "planning_failed",
    "source": "architect",
    "detail": CAUSE,
    "observed_at": "2026-09-26T10:00:00+00:00",
}
RETRYING_STORY = {
    "id": "story-92b433c8",
    "title": "Recipe bot",
    "type": "product",
    "status": "in_progress",
    "updated_at": "2026-09-26T10:00:00+00:00",
    "quarantine_reason": None,
    "planning": {
        "state": "retrying",
        "failed_attempts": 1,
        "max_retries": 3,
        "next_attempt_at": "2026-09-26T10:01:00+00:00",
        "last_failure": FAILURE,
        "channels": [],
        "channel_failures": ["codex:rate_limited"],
        "recorded_at": "2026-09-26T10:00:00+00:00",
    },
}
PARKED_STORY = {
    **RETRYING_STORY,
    "status": "waiting_human_review",
    "quarantine_reason": FAILURE,
    "planning": {**RETRYING_STORY["planning"], "state": "parked", "next_attempt_at": None},
}


def _response(data, status_code: int = 200) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.json.return_value = data
    return resp


def _diagnostics(story: dict) -> dict:
    return {
        "story_id": story["id"],
        "project_id": "11111111-1111-4111-8111-111111111111",
        "story_status": story["status"],
        "project_status": "active",
        "failure": story["quarantine_reason"],
        "quarantine_reason": None,
        "scaffold_error": None,
        # The failed attempt's leftovers: tasks exist, nothing is being built.
        "work_cycle_tasks": 3,
        "failed_runs": [],
        "task_failures": [],
        "logs": [],
        "logs_unavailable": "not requested",
    }


@pytest.fixture
def api():
    client = AsyncMock(spec=InternalAPIClient)
    init_po_clients(client, AsyncMock())
    return client


def _config() -> dict:
    return {"configurable": {"thread_id": "po-chat-42", "telegram_chat_id": "42"}}


async def _get_story(api, story: dict) -> dict:
    api.get_raw.side_effect = [_response(story), _response([]), _response(_diagnostics(story))]
    return json.loads(await get_story.ainvoke({"story_id": story["id"]}, config=_config()))


@pytest.mark.asyncio
async def test_a_retrying_story_is_a_problem_with_its_attempts_and_reason(api):
    problem = (await _get_story(api, RETRYING_STORY))["problem"]

    assert problem is not None
    assert "planning the work failed" in problem
    assert "1 of 3" in problem
    assert "a problem the platform is handling" in problem
    assert "retrying it automatically" in problem
    assert CAUSE in problem


@pytest.mark.asyncio
async def test_a_parked_story_is_a_problem_that_waits_for_an_operator(api):
    problem = (await _get_story(api, PARKED_STORY))["problem"]

    assert problem is not None
    assert "planning the work failed" in problem
    assert "a problem the platform is handling" in problem
    assert "until an operator re-runs planning" in problem
    assert CAUSE in problem


@pytest.mark.asyncio
async def test_the_retrying_state_is_reported_even_without_diagnostics(api):
    api.get_raw.side_effect = [
        _response(RETRYING_STORY),
        _response([]),
        _response({"detail": "boom"}, status_code=503),
    ]

    parsed = json.loads(await get_story.ainvoke({"story_id": "story-92b433c8"}, config=_config()))

    assert "retrying it automatically" in parsed["problem"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("story", "words"),
    [(RETRYING_STORY, "retrying it automatically"), (PARKED_STORY, "until an operator")],
)
async def test_the_story_overview_marks_a_failed_planning_as_a_problem(api, story, words):
    api.get_raw.return_value = _response([story])

    listing = await list_stories.ainvoke({"project_id": "p-1"}, config=_config())

    assert "PROBLEM: planning the work failed" in listing
    assert words in listing


@pytest.mark.asyncio
async def test_a_planned_story_is_no_problem(api):
    planned = {
        **RETRYING_STORY,
        "planning": {**RETRYING_STORY["planning"], "state": "planned", "last_failure": None},
    }
    api.get_raw.return_value = _response([planned])

    assert "PROBLEM" not in await list_stories.ainvoke({"project_id": "p-1"}, config=_config())


@pytest.mark.parametrize("tool", [get_story, list_stories])
def test_the_po_is_told_to_report_it_as_a_problem_never_as_in_progress(tool):
    text = " ".join(tool.description.split())

    assert "a problem the platform is handling" in text
    assert '"in progress, no errors"' in text
