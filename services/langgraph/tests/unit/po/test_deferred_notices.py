"""Scripted PO turns exercise the tools and the actual proactive publish point."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from fakeredis.aioredis import FakeRedis
import httpx
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.owner_notification import (
    AddressedOwnerNotice,
    OwnerNoticeReference,
    OwnerNoticeSettlement,
    OwnerNotification,
)
from shared.contracts.queues.po import POSystemEvent, POUserMessage, po_thread_id, to_flat_fields
from src.agents.po import tools_notices, tools_shared
from src.agents.po.graph import po_prompt
from src.agents.po.situation import ApiSituationReader
from src.agents.po.tools import notify_user
from src.agents.po.tools_stories import get_product_situation
from src.clients.api import LanggraphAPIClient
from src.consumers import po
from tests.unit.po.situation_api import project_body

CHAT = "1015926438"
STORY = "story-order"
PROJECT = "00000000-0000-0000-0000-000000000001"


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])


def graph(*responses):
    return create_react_agent(
        ScriptedModel(responses=list(responses)),
        [
            tools_notices.suppress_owner_notice,
            tools_notices.resolve_deferred_notice,
            get_product_situation,
            notify_user,
        ],
        prompt=po_prompt,
        checkpointer=MemorySaver(),
    )


class NoticeAPI:
    """Transport ledger; database fences and admin delivery are tested in the API suite."""

    def __init__(self, base):
        self.base = base
        self.notice = OwnerNotification(
            event="story_blocked",
            text="The optional report is stopped",
            story_id=STORY,
            project_id=PROJECT,
            terminal_status="waiting_human_review",
            state="delivered",
            owed_at=datetime.now(UTC) - timedelta(days=40),
            delivered_at=datetime.now(UTC),
        )
        self.source = "run"
        self.source_id = "run-1"
        self.writes = []
        self.fail_write = False
        self.fail_read = False

    def addressed(self):
        return AddressedOwnerNotice(
            source=self.source,
            source_id=self.source_id,
            owed_at=self.notice.owed_at,
            notification=self.notice,
        )

    async def handle(self, request):
        path = request.url.path
        if path == f"/api/stories/{STORY}/owner-notifications":
            if self.fail_read:
                return httpx.Response(503, json={"detail": "temporarily unavailable"})
            return httpx.Response(200, json=[self.addressed().model_dump(mode="json")])
        if path == "/api/stories/owner-notifications/deferred":
            rows = (
                [self.addressed().model_dump(mode="json")]
                if self.notice.told_state == "suppressed"
                else []
            )
            return httpx.Response(200, json=rows)
        if path == f"/api/stories/{STORY}/owner-notifications/settlement":
            command = OwnerNoticeSettlement.model_validate_json(request.content)
            self.writes.append(command)
            if self.fail_write:
                return httpx.Response(409, json={"detail": "Owner notice was replaced"})
            if (
                self.notice.event == "story_waiting_user_secret"
                and command.told_state == "suppressed"
            ):
                return httpx.Response(409, json={"detail": "Only the user can supply this secret"})
            fields = {"told_state": command.told_state}
            if command.told_state == "suppressed":
                fields.update(
                    suppressed_reason=command.reason,
                    suppressed_by=command.suppressed_by,
                    suppressed_at=datetime.now(UTC),
                )
            else:
                fields[f"{command.told_state}_at"] = datetime.now(UTC)
                if command.told_state == "closed":
                    fields["closed_reason"] = command.reason
            self.notice = self.notice.model_copy(update=fields)
            return httpx.Response(200, json=self.notice.model_dump(mode="json"))
        return await self.base._handle(request)

    def event(self, *, durable=True):
        return to_flat_fields(
            POSystemEvent(
                event=self.notice.event,
                text=self.notice.text,
                story_id=STORY,
                project_id=PROJECT,
                telegram_chat_id=CHAT,
                owner_notice=(
                    OwnerNoticeReference(
                        source=self.source,
                        source_id=self.source_id,
                        owed_at=self.notice.owed_at,
                    )
                    if durable
                    else None
                ),
            )
        )


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


def user(text, request="user-1"):
    return POUserMessage(text=text, telegram_chat_id=CHAT, request_id=request).model_dump(
        mode="json"
    )


async def test_user_preference_defers_then_return_retrieves_and_resolves(world):
    ledger, stream = world
    model_graph = graph(
        AIMessage(content="I will defer small notices."),
        call(
            "suppress_owner_notice",
            story_id=STORY,
            reason="Don't write about small things",
            decided_by="user",
        ),
        AIMessage(content="This reply must not be published."),
        call("get_product_situation", project_id=PROJECT),
        call("notify_user", message="The optional report stopped while you were away."),
        call("resolve_deferred_notice", story_id=STORY, outcome="told", reason="Told on return"),
        call("get_product_situation", project_id=PROJECT),
        AIMessage(content="The optional report stopped while you were away."),
    )
    await po._handle_message(
        model_graph, stream, CHAT, user("Don't write to me about small things")
    )
    await po._handle_message(model_graph, stream, CHAT, ledger.event())
    assert ledger.notice.told_state == "suppressed"
    assert ledger.notice.suppressed_by == "user"
    assert ledger.notice.suppressed_reason == "Don't write about small things"
    assert "po:proactive" not in [c.args[0] for c in stream.publish_flat.call_args_list]

    await po._handle_message(model_graph, stream, CHAT, user("I'm back. What happened?", "return"))
    state = await model_graph.aget_state({"configurable": {"thread_id": po_thread_id(CHAT)}})
    snapshots = [
        m.content
        for m in state.values["messages"]
        if isinstance(m, ToolMessage) and m.name == "get_product_situation"
    ]
    assert ledger.notice.text in snapshots[0]
    assert ledger.notice.suppressed_reason in snapshots[0]
    assert "decided by=user" in snapshots[0]
    assert snapshots[1].endswith("### Deferred notices\nnone")
    assert ledger.notice.told_state == "told"


@pytest.mark.parametrize("source", ["run", "story"])
async def test_publication_marks_the_exact_record_told(world, source):
    ledger, stream = world
    ledger.source, ledger.source_id = source, STORY if source == "story" else "run-1"
    await po._handle_message(
        graph(AIMessage(content="Work is stopped.")), stream, CHAT, ledger.event()
    )
    assert stream.publish_flat.call_args.args[0] == "po:proactive"
    assert ledger.notice.told_at is not None
    assert ledger.writes[0].owed_at == ledger.notice.owed_at
    assert ledger.writes[0].source == source


async def test_failed_told_write_is_logged_without_undoing_publish(world):
    ledger, stream = world
    ledger.fail_write = True
    with capture_logs() as logs:
        await po._handle_message(graph(AIMessage(content="Stopped")), stream, CHAT, ledger.event())
    assert stream.publish_flat.call_args.args[0] == "po:proactive"
    assert any(log["event"] == "po_owner_notice_told_write_failed" for log in logs)


async def test_best_effort_latest_event_refuses_to_suppress_an_older_record(world):
    ledger, stream = world
    scripted = graph(
        call("suppress_owner_notice", story_id=STORY, reason="small"),
        AIMessage(content="Tell the best-effort event."),
    )
    await po._handle_message(scripted, stream, CHAT, ledger.event(durable=False))
    assert ledger.writes == []
    state = await scripted.aget_state({"configurable": {"thread_id": po_thread_id(CHAT)}})
    assert any("tell it, or note it to the admins" in m.content for m in state.values["messages"])
    assert stream.publish_flat.call_args.args[0] == "po:proactive"


async def test_waiting_secret_refusal_still_publishes_the_question(world):
    ledger, stream = world
    ledger.notice = ledger.notice.model_copy(update={"event": "story_waiting_user_secret"})
    scripted = graph(
        call("suppress_owner_notice", story_id=STORY, reason="small"),
        AIMessage(content="Please provide the secret."),
    )
    await po._handle_message(scripted, stream, CHAT, ledger.event())
    assert stream.publish_flat.call_args.args[0] == "po:proactive"
    assert ledger.notice.told_state == "told"


async def test_admin_deferral_at_publish_point_withholds_reply(world):
    ledger, stream = world
    ledger.notice = ledger.notice.model_copy(
        update={
            "told_state": "suppressed",
            "suppressed_by": "admin",
            "suppressed_reason": "Wait",
            "suppressed_at": datetime.now(UTC),
        }
    )
    await po._handle_message(
        graph(AIMessage(content="Do not publish")), stream, CHAT, ledger.event()
    )
    stream.publish_flat.assert_not_called()


async def test_empty_reason_and_admin_tool_decider_are_refused(world):
    config = {"configurable": {"telegram_chat_id": CHAT, "user_turn": True}}
    answer = await tools_notices.suppress_owner_notice.ainvoke(
        {"story_id": STORY, "reason": "  "},
        config=config,
    )
    assert "non-empty reason" in answer
    schema = tools_notices.suppress_owner_notice.tool_call_schema.model_json_schema()
    assert schema["properties"]["decided_by"]["enum"] == ["po", "user"]


async def test_unreadable_publication_decision_stays_pending(world):
    ledger, stream = world
    ledger.fail_read = True
    stream.redis.xack = AsyncMock()
    await po._process_message(
        graph(AIMessage(content="Stopped")),
        stream,
        asyncio.Semaphore(1),
        {},
        "1-0",
        POSystemEvent.model_validate(ledger.event()),
    )
    stream.publish_flat.assert_not_awaited()
    stream.redis.xack.assert_not_awaited()


async def test_explicit_close_requires_reason_and_removes_deferral(world):
    ledger, _stream = world
    ledger.notice = ledger.notice.model_copy(
        update={
            "told_state": "suppressed",
            "suppressed_by": "po",
            "suppressed_reason": "Wait",
            "suppressed_at": datetime.now(UTC),
        }
    )
    config = {"configurable": {"telegram_chat_id": CHAT, "user_turn": True}}
    empty = await tools_notices.resolve_deferred_notice.ainvoke(
        {"story_id": STORY, "outcome": "closed", "reason": " "},
        config=config,
    )
    assert "requires" in empty
    assert ledger.notice.told_state == "suppressed"
    answer = await tools_notices.resolve_deferred_notice.ainvoke(
        {"story_id": STORY, "outcome": "closed", "reason": "User said drop it"},
        config=config,
    )
    assert "recorded as closed" in answer
    snapshot = await get_product_situation.ainvoke({"project_id": PROJECT}, config=config)
    assert snapshot.endswith("### Deferred notices\nnone")


async def test_resolving_told_requires_a_user_turn(world):
    answer = await tools_notices.resolve_deferred_notice.ainvoke(
        {"story_id": STORY, "outcome": "told"},
        config={"configurable": {"telegram_chat_id": CHAT, "user_turn": False}},
    )
    assert "user turn" in answer
    assert world[0].writes == []
