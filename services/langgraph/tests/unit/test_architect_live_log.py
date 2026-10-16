"""A claimed planning attempt's live log: graph lines through the callback, one consumer end."""

from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.agent_log import AGENT_LOG_LINE_EVENT, AgentLogLine, AgentLogWriter
from shared.contracts.dto.llm_channel import LLMChannel
from shared.contracts.dto.product_brief import ProductBriefAdmissionOutcome
from src.agents.architect.graph import create_architect_graph
from src.agents.architect.live_log import ArchitectLiveLog
from tests.unit.factories import make_admission, make_story
from tests.unit.test_architect_graph import _ScriptedToolCallingModel
from tests.unit.test_architect_planning_failure import (
    EVERY_CHANNEL_UNFIXABLE,
    _brief,
    _exhausted,
    _graph,
    _PlanningAPI,
    _run,
)

SECRET = "s3cr3t-planning-value"  # noqa: S105 - the value the log must redact
STORY_TITLE = "Story title only the tool result carries"


def _tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}])


def _state() -> dict:
    return {
        "messages": [{"role": "user", "content": "Decompose story story-abc."}],
        "story_id": "story-abc",
        "project_id": "proj-1",
        "telegram_chat_id": "chat-1",
        "product_brief_id": "brief-1",
        "planning_attempt_id": "plan-1",
        "must_requirements": [],
    }


async def _run_graph(model, api) -> None:  # noqa: ANN001
    writer = AgentLogWriter(attempt_id="plan-1", task_id=None, secrets=[SECRET])
    graph = create_architect_graph(model)
    with patch("src.agents.architect.tools.api_client", api):
        await graph.ainvoke(_state(), config={"callbacks": [ArchitectLiveLog(writer)]})


def _lines(logs: list[dict]) -> list[AgentLogLine]:
    return [
        AgentLogLine.model_validate(
            {key: value for key, value in entry.items() if key not in {"event", "log_level"}}
        )
        for entry in logs
        if entry["event"] == AGENT_LOG_LINE_EVENT
    ]


def _planned_by_model() -> object:
    return create_architect_graph(_ScriptedToolCallingModel(turns=[AIMessage(content="Planned.")]))


class _IncompleteAdmissionAPI(_PlanningAPI):
    """An API that refuses the plan: a must-requirement is left undisposed."""

    async def admit_product_brief_coverage(self, brief_id, planning_attempt_id, **_kwargs):
        return make_admission(
            outcome=ProductBriefAdmissionOutcome.INCOMPLETE,
            coverage_admitted_at=None,
            released_task_ids=[],
            missing_requirement_ids=["req-1"],
        )


@pytest.mark.asyncio
async def test_graph_logs_tool_call_result_and_message_in_order():
    logged_before_tool: list[int] = []
    api = AsyncMock()

    async def get_story(story_id: str):
        logged_before_tool.append(len(_lines(logs)))
        return make_story(id=story_id, title=STORY_TITLE)

    api.get_story = AsyncMock(side_effect=get_story)
    model = _ScriptedToolCallingModel(
        turns=[
            _tool_call("get_story", {"story_id": f"story-{SECRET}"}),
            AIMessage(content="Planned."),
        ]
    )
    with capture_logs() as logs:
        await _run_graph(model, api)

    lines = _lines(logs)
    assert [(line.seq, line.kind, line.text) for line in lines] == [
        (0, "cmd", 'get_story {"story_id": "story-[redacted]"}'),
        (1, "ok", "get_story"),
        (2, "info", "Planned."),
    ]
    assert {(line.attempt_id, line.task_id) for line in lines} == {("plan-1", None)}
    assert logged_before_tool == [1]
    assert not any(STORY_TITLE in line.text or SECRET in line.text for line in lines)


@pytest.mark.asyncio
async def test_graph_logs_a_refused_tool_answer_as_err():
    api = AsyncMock()
    api.get_project = AsyncMock(return_value=None)
    model = _ScriptedToolCallingModel(
        turns=[
            _tool_call("get_project_spec", {"project_id": "proj-1"}),
            AIMessage(content="Planned."),
        ]
    )

    with capture_logs() as logs:
        await _run_graph(model, api)

    assert [(line.kind, line.text) for line in _lines(logs)] == [
        ("cmd", 'get_project_spec {"project_id": "proj-1"}'),
        ("err", "get_project_spec"),
        ("info", "Planned."),
    ]


@pytest.mark.asyncio
async def test_admitted_attempt_ends_with_one_admitted_step_after_the_model_lines():
    with capture_logs() as logs:
        result = await _run(_PlanningAPI(brief=_brief()), _planned_by_model())

    assert result["status"] == "success"
    assert [(line.attempt_id, line.seq, line.kind, line.text) for line in _lines(logs)] == [
        ("plan-1", 0, "info", "Planned."),
        ("plan-1", 1, "step", "planning ended: admitted"),
    ]


@pytest.mark.asyncio
async def test_attempt_the_api_does_not_admit_ends_not_admitted():
    with capture_logs() as logs:
        result = await _run(_IncompleteAdmissionAPI(brief=_brief()), _graph(LLMChannel.CODEX))

    assert result["status"] == "incomplete"
    assert [(line.kind, line.text) for line in _lines(logs)] == [
        ("step", "planning ended: not admitted (ProductBriefCoverageIncomplete)"),
    ]


@pytest.mark.asyncio
async def test_attempt_whose_planning_raised_ends_with_the_error_class():
    with capture_logs() as logs:
        result = await _run(
            _PlanningAPI(brief=_brief()), _graph(_exhausted(*EVERY_CHANNEL_UNFIXABLE))
        )

    assert result["status"] == "failed"
    assert [(line.kind, line.text) for line in _lines(logs)] == [
        ("step", "planning ended: error (LLMChannelsExhausted)"),
    ]


@pytest.mark.asyncio
async def test_planning_without_a_claimed_attempt_logs_nothing():
    with capture_logs() as logs:
        result = await _run(_PlanningAPI(), _planned_by_model())

    assert result["status"] == "success"
    assert _lines(logs) == []
