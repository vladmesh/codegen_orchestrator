"""DoD8 through the real consumer and PO graph, with a scripted model.

A fresh block of an ordered story is told without softening: the event reaches
the graph undeferred, the snapshot the model reads says the story is stopped, a
person is needed and there is no known deadline, and the reply is published.
A complaint about a completed order reopens that order; it never starts a new
story.
"""

from datetime import UTC, datetime, timedelta
import json
from unittest.mock import AsyncMock

from fakeredis.aioredis import FakeRedis
import httpx
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import Field
import pytest

from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.po import POUserMessage, po_thread_id
from shared.queues import ARCHITECT_QUEUE, PO_PROACTIVE_QUEUE
from src.agents.po import tools_notices, tools_shared
from src.agents.po.graph import po_prompt
from src.agents.po.situation import ApiSituationReader
from src.agents.po.tools_stories import create_story, list_stories, reopen_story
from src.clients.api import LanggraphAPIClient
from src.consumers import po
from tests.unit.po.situation_api import project_body
from tests.unit.po.test_deferred_notices import (
    CHAT,
    PROJECT,
    STORY,
    NoticeAPI,
    ScriptedModel,
    call,
)


class RecordingModel(ScriptedModel):
    """A scripted model that keeps every message list it was invoked with."""

    seen: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _graph(model, tools):
    return create_react_agent(model, tools, prompt=po_prompt, checkpointer=MemorySaver())


def _line(text: str, label: str) -> str:
    [line] = [line for line in text.splitlines() if line.startswith(f"- {label}:")]
    return line.removeprefix(f"- {label}:").strip()


@pytest.fixture
def world(monkeypatch, situation_api):
    situation_api.projects = [project_body(PROJECT)]
    situation_api.project_story_ids = {PROJECT: [STORY]}
    ledger = NoticeAPI(situation_api)
    situation_api.client._client = httpx.AsyncClient(
        base_url="http://api.test", transport=httpx.MockTransport(ledger.handle)
    )
    stream = AsyncMock()
    stream.redis = FakeRedis(decode_responses=True)
    monkeypatch.setattr(tools_shared, "_api_client", situation_api.client)
    monkeypatch.setattr(tools_shared, "_stream_client", stream)
    consumer_api = LanggraphAPIClient()
    consumer_api._client = situation_api.client._client
    monkeypatch.setattr(po, "api_client", consumer_api)
    monkeypatch.setattr(po, "_situation_reader", lambda: ApiSituationReader(situation_api.client))
    return ledger, stream


async def test_a_fresh_block_of_an_ordered_story_is_told_without_softening(world, gate_stories):
    ledger, stream = world
    now = datetime.now(UTC)
    blocked = now - timedelta(minutes=4)
    gate_stories.put(
        STORY,
        title="Expense tracking",
        status="waiting_human_review",
        waiting_on="human_review",
        status_entered_at=blocked.isoformat(),
    )
    ledger.notice = ledger.notice.model_copy(
        update={
            "text": "Work on this change is stopped; a person has to resolve it.",
            "owed_at": blocked,
            "delivered_at": blocked,
        }
    )
    reply = "Work on expense tracking is stopped. A person has to resolve it; no date yet."
    model = RecordingModel(responses=[AIMessage(content=reply)])

    await po._handle_message(
        _graph(model, [tools_notices.suppress_owner_notice]), stream, CHAT, ledger.event()
    )

    # The event reached the graph: an ordered story, not deferred by anybody.
    [messages] = model.seen
    assert "[system: system_event:story_blocked]" in messages[-1].content
    system = messages[0]
    assert isinstance(system, SystemMessage)
    assert _line(system.content, "Order").startswith("ordered, Product Brief confirmed")
    assert _line(system.content, "Status") == (
        "waiting_human_review, waiting on human_review; stopped, a person is needed, "
        "no known deadline"
    )
    assert _line(system.content, "In this status since") == (
        f"{blocked:%Y-%m-%d %H:%M} UTC (4 minutes ago)"
    )
    assert system.content.endswith("### Deferred notices\nnone")
    # The scripted reply is what the user is sent, and the record is told.
    queue, fields = stream.publish_flat.await_args.args
    assert queue == PO_PROACTIVE_QUEUE
    assert fields["text"] == reply
    assert ledger.notice.told_state == "told"


class _Orders:
    """The user's one completed order, as the story endpoints answer for it."""

    def __init__(self, base) -> None:
        self.base = base
        self.story = {
            "id": STORY,
            "project_id": PROJECT,
            "title": "Expense tracking",
            "type": "product",
            "status": "completed",
        }
        self.reopened: list[dict] = []
        self.created: list[dict] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == "/api/stories/":
            assert request.url.params["project_id"] == PROJECT
            return httpx.Response(200, json=[self.story])
        if method == "POST" and path == "/api/stories/":
            self.created.append(request.read().decode())
            return httpx.Response(500, json={"detail": "no new story expected"})
        if method == "GET" and path == f"/api/stories/{STORY}":
            return httpx.Response(200, json=self.story)
        if method == "POST" and path == f"/api/stories/{STORY}/reopen":
            self.reopened.append(json.loads(request.content))
            self.story = {**self.story, "status": "reopened"}
            return httpx.Response(200, json=self.story)
        return await self.base._handle(request)


async def test_a_complaint_about_a_completed_order_reopens_it_and_creates_nothing(
    world, situation_api
):
    _ledger, stream = world
    orders = _Orders(situation_api)
    situation_api.client._client = httpx.AsyncClient(
        base_url="http://api.test", transport=httpx.MockTransport(orders.handle)
    )
    complaint = "The expense bot stopped answering /report"
    model = RecordingModel(
        responses=[
            call("list_stories", project_id=PROJECT),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "reopen_story",
                        "args": {"story_id": STORY, "user_report": complaint},
                        "id": "reopen_story",
                    }
                ],
            ),
            AIMessage(content="I reopened your order with your report."),
        ]
    )
    graph = _graph(model, [list_stories, reopen_story, create_story])
    turn = POUserMessage(text=complaint, telegram_chat_id=CHAT, request_id="complaint-1")

    await po._handle_message(graph, stream, CHAT, turn.model_dump(mode="json"))

    assert orders.reopened == [{"user_report": complaint, "actor": "po"}]
    assert orders.created == []
    assert orders.story["status"] == "reopened"
    [(queue, message)] = [c.args for c in stream.publish_message.await_args_list]
    assert queue == ARCHITECT_QUEUE
    assert isinstance(message, ArchitectMessage)
    assert (message.story_id, message.is_reopen, message.user_report) == (STORY, True, complaint)
    state = await graph.aget_state({"configurable": {"thread_id": po_thread_id(CHAT)}})
    called = [
        tool_call["name"]
        for msg in state.values["messages"]
        for tool_call in getattr(msg, "tool_calls", None) or []
    ]
    assert called == ["list_stories", "reopen_story"]
    queue, _fields = stream.publish_flat.await_args.args
    assert queue == "po:response:complaint-1"
