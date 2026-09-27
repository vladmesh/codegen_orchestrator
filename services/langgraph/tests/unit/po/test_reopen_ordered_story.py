"""A retry after a failure reopens the user's order; it never starts a second story.

The retry's outcome is told to the owner as the completion of their order: the
story keeps its confirmed brief across the reopen, so it stays ordered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
from langchain_core.messages import AIMessage
import pytest

from shared.contracts.dto.product_brief import ProductBriefRead
from shared.contracts.queues.architect import ArchitectMessage
from shared.contracts.queues.po import POSystemEvent
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import ARCHITECT_QUEUE, PO_PROACTIVE_QUEUE
from src.agents.po.tools_shared import init_po_clients
from src.agents.po.tools_stories import reopen_story
from src.consumers import po as po_consumer
from src.consumers.po import _handle_message
from tests.unit.factories import make_product_brief

CHAT = "user-42"
STORY = "story-order"


def _response(data: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.is_success = True
    resp.json.return_value = data
    return resp


class _Api:
    """The stories and the one brief bound to the ordered story, as the API keeps them."""

    def __init__(self) -> None:
        self.stories = {
            STORY: {"id": STORY, "title": "Recipe bot", "project_id": "proj-1", "status": "failed"}
        }
        self.briefs = {STORY: make_product_brief(story_id=STORY)}

    async def get_raw(self, path: str, headers=None, **kwargs) -> MagicMock:
        return _response(self.stories[path.removeprefix("stories/")])

    async def post_raw(self, path: str, json=None, headers=None, **kwargs) -> MagicMock:
        story_id = path.removeprefix("stories/").removesuffix("/reopen")
        story = self.stories[story_id]
        assert story["status"] in {"completed", "failed"}
        story["status"] = "reopened"
        return _response(story)

    async def get_product_brief_by_story(self, story_id: str) -> ProductBriefRead | None:
        return self.briefs.get(story_id)


@pytest.mark.asyncio
async def test_a_reopened_failed_order_is_one_story_whose_completion_the_owner_hears(
    monkeypatch,
):
    api = _Api()
    stream = AsyncMock()
    init_po_clients(api, stream)
    monkeypatch.setattr(
        po_consumer.api_client, "get_product_brief_by_story", api.get_product_brief_by_story
    )
    admins = AsyncMock()
    monkeypatch.setattr(po_consumer, "notify_admins_best_effort", admins)

    await reopen_story.ainvoke(
        {"story_id": STORY},
        config={"configurable": {"telegram_chat_id": CHAT, "user_turn": True}},
    )

    # One story, reopened in place, and the architect plans that same story again.
    assert list(api.stories) == [STORY]
    assert api.stories[STORY]["status"] == "reopened"
    queue, message = stream.publish_message.await_args.args
    assert queue == ARCHITECT_QUEUE
    assert isinstance(message, ArchitectMessage)
    assert (message.story_id, message.project_id, message.is_reopen, message.user_report) == (
        STORY,
        "proj-1",
        True,
        None,
    )
    # The confirmed brief stays bound across the reopen: the story is still an order.
    assert (await api.get_product_brief_by_story(STORY)).confirmed_at is not None

    api.stories[STORY]["status"] = "completed"
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="Your recipe bot is live.")]}
    graph.aget_state.return_value = MagicMock(values={"messages": []})
    po_stream = AsyncMock()

    await _handle_message(
        graph,
        po_stream,
        CHAT,
        POSystemEvent(
            event=OwnerNotificationEvent.STORY_COMPLETED,
            text="The story is finished. The bot is @recipe_bot.",
            story_id=STORY,
            project_id="proj-1",
            telegram_chat_id=CHAT,
        ).model_dump(mode="json"),
    )

    graph.ainvoke.assert_called_once()
    assert "system_event:story_completed" in graph.ainvoke.call_args.args[0]["messages"][0].content
    stream_name, fields = po_stream.publish_flat.await_args.args
    assert stream_name == PO_PROACTIVE_QUEUE
    assert fields["story_id"] == STORY
    assert fields["event"] == OwnerNotificationEvent.STORY_COMPLETED
    admins.assert_not_called()
