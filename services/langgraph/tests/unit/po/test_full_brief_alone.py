"""`show_full_brief` in a turn with other tool calls: the brief is still the reply.

The tool is `return_direct`, so the turn ends on the last tool message and the
consumer sends that message to the user. These drive the graph the consumer
builds, with a scripted model, and read what the user would be sent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from shared.contracts.queues.po import MESSAGE_BREAK
from shared.product_brief_text import full_brief_unavailable
from src.agents.po.graph import create_po_graph
from src.agents.po.tools_shared import init_po_clients
from tests.unit.po.test_tools_briefs import _API, BRIEF_ID, PROJECT_ID, _brief
from tests.unit.test_architect_graph import _ScriptedToolCallingModel

SHOW = {"name": "show_full_brief", "args": {"brief_id": BRIEF_ID}, "id": "call-show"}
CONFIRM = {
    "name": "confirm_product_brief",
    "args": {"project_id": PROJECT_ID, "brief_id": BRIEF_ID},
    "id": "call-confirm",
}
LIST_PROJECTS = {"name": "list_projects", "args": {}, "id": "call-list"}


async def _turn(api: _API, calls: list[dict], *, then: str = "re-typed text") -> list:
    init_po_clients(api, AsyncMock())
    model = _ScriptedToolCallingModel(
        turns=[AIMessage(content="", tool_calls=calls), AIMessage(content=then)]
    )
    config = {"configurable": {"thread_id": "po-full-brief-alone", "telegram_chat_id": "42"}}
    with patch("src.agents.po.graph.ChatOpenAI", return_value=model):
        graph = await create_po_graph(model="scripted", base_url="http://llm.invalid", api_key="x")
    state = await graph.ainvoke(
        {"messages": [HumanMessage(content="Show me the whole brief and confirm it")]},
        config=config,
    )
    return state["messages"]


@pytest.mark.parametrize(
    "calls",
    [[SHOW, CONFIRM], [CONFIRM, SHOW], [LIST_PROJECTS, SHOW, CONFIRM]],
    ids=["brief-first", "brief-last", "brief-between"],
)
async def test_the_full_brief_is_the_reply_and_the_other_calls_never_run(calls):
    api = _API(briefs={BRIEF_ID: _brief()})

    messages = await _turn(api, calls)

    last = messages[-1]
    assert isinstance(last, ToolMessage)
    assert last.name == "show_full_brief"
    assert last.content.split(MESSAGE_BREAK)[0].startswith("<b>Recipe bot</b>")
    # The confirmation was answered with a short tool error and never sent.
    assert api.posts == []
    refused = {
        message.name: message
        for message in messages
        if isinstance(message, ToolMessage) and message.name != "show_full_brief"
    }
    assert set(refused) == {call["name"] for call in calls} - {"show_full_brief"}
    for message in refused.values():
        assert message.status == "error"
        assert message.content.startswith(f"Not run: {message.name} was called")
    # Every call of the turn has exactly one answer, so the thread stays valid.
    answered = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert sorted(answered) == sorted(call["id"] for call in calls)


async def test_a_brief_that_cannot_be_read_beside_another_call_is_the_apology():
    api = _API()

    messages = await _turn(api, [SHOW, CONFIRM])

    last = messages[-1]
    assert isinstance(last, ToolMessage)
    assert last.name == "show_full_brief"
    assert last.content == full_brief_unavailable(None)
    assert api.posts == []


@pytest.mark.parametrize(
    "arguments", [{}, {"brief_id": 7}, {"brief": BRIEF_ID}], ids=["none", "not-a-string", "wrong"]
)
async def test_unusable_arguments_never_reach_the_user_as_validation_text(arguments):
    """The tool node would answer them with raw validation text, and end the turn on it."""
    api = _API(briefs={BRIEF_ID: _brief()})
    show = {"name": "show_full_brief", "args": arguments, "id": "call-show"}

    messages = await _turn(api, [show, CONFIRM], then="Which brief should I show?")

    # Nothing ran; the model got another step and wrote the reply itself.
    last = messages[-1]
    assert isinstance(last, AIMessage)
    assert last.content == "Which brief should I show?"
    assert api.posts == []
    refusal = next(m for m in messages if isinstance(m, ToolMessage) and m.name == SHOW["name"])
    assert refusal.status == "error"
    assert refusal.content.startswith("Not run: show_full_brief needs `brief_id`")


async def test_a_turn_without_the_full_brief_runs_every_call():
    api = _API(briefs={BRIEF_ID: _brief()})

    messages = await _turn(api, [CONFIRM], then="Confirmed.")

    assert messages[-1].content == "Confirmed."
    assert [path for path, _ in api.posts] == [f"product-briefs/{BRIEF_ID}/confirm"]
