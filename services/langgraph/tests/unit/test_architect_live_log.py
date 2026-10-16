"""The architect graph's planning run writes its live log lines through the callback seam."""

from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, ToolMessage
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.agent_log import AGENT_LOG_LINE_EVENT, AgentLogLine
from src.agents.architect.graph import create_architect_graph
from src.agents.architect.live_log import ArchitectLiveLog
from tests.unit.factories import make_story
from tests.unit.test_architect_graph import _ScriptedToolCallingModel

SECRET = "s3cr3t-planning-value"  # noqa: S105 - the value the log must redact
STORY_TITLE = "Story title only the tool result carries"


class _FailingAfterToolModel(_ScriptedToolCallingModel):
    """Answers its scripted turn, then fails once a tool result is in the history."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        if any(isinstance(message, ToolMessage) for message in messages):
            raise RuntimeError("every channel failed")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _tool_call(name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}])


def _state(planning_attempt_id: str | None) -> dict:
    return {
        "messages": [{"role": "user", "content": "Decompose story story-abc."}],
        "story_id": "story-abc",
        "project_id": "proj-1",
        "telegram_chat_id": "chat-1",
        "product_brief_id": None if planning_attempt_id is None else "brief-1",
        "planning_attempt_id": planning_attempt_id,
        "must_requirements": [],
    }


async def _plan(model, planning_attempt_id: str | None, api) -> None:  # noqa: ANN001
    graph = create_architect_graph(model)
    with patch("src.agents.architect.tools.api_client", api):
        await graph.ainvoke(
            _state(planning_attempt_id),
            config={"callbacks": [ArchitectLiveLog(secrets=[SECRET])]},
        )


def _lines(logs: list[dict]) -> list[AgentLogLine]:
    return [
        AgentLogLine.model_validate(
            {key: value for key, value in entry.items() if key not in {"event", "log_level"}}
        )
        for entry in logs
        if entry["event"] == AGENT_LOG_LINE_EVENT
    ]


@pytest.mark.asyncio
async def test_planning_run_logs_tool_call_result_message_and_end_in_order():
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
        await _plan(model, "plan-1", api)

    lines = _lines(logs)
    assert [(line.seq, line.kind, line.text) for line in lines] == [
        (0, "cmd", 'get_story {"story_id": "story-[redacted]"}'),
        (1, "ok", "get_story"),
        (2, "info", "Planned."),
        (3, "step", "planning ended: success"),
    ]
    assert {(line.attempt_id, line.task_id) for line in lines} == {("plan-1", None)}
    assert logged_before_tool == [1]
    assert not any(STORY_TITLE in line.text or SECRET in line.text for line in lines)


@pytest.mark.asyncio
async def test_refused_tool_answer_and_model_failure_end_planning_as_errors():
    api = AsyncMock()
    api.get_project = AsyncMock(return_value=None)
    model = _FailingAfterToolModel(turns=[_tool_call("get_project_spec", {"project_id": "proj-1"})])

    with capture_logs() as logs, pytest.raises(RuntimeError, match="every channel failed"):
        await _plan(model, "plan-1", api)

    lines = _lines(logs)

    assert [(line.kind, line.text) for line in lines] == [
        ("cmd", 'get_project_spec {"project_id": "proj-1"}'),
        ("err", "get_project_spec"),
        ("step", "planning ended: error (RuntimeError)"),
    ]


@pytest.mark.asyncio
async def test_planning_run_without_claimed_attempt_logs_nothing():
    api = AsyncMock()
    api.get_story = AsyncMock(return_value=make_story(title=STORY_TITLE))
    model = _ScriptedToolCallingModel(
        turns=[_tool_call("get_story", {"story_id": "story-abc"}), AIMessage(content="Planned.")]
    )

    with capture_logs() as logs:
        await _plan(model, None, api)

    assert _lines(logs) == []
