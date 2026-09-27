"""Only an ordered story's outcome reaches the user; every other story's goes to the admins.

Incident: a ``story_blocked`` for ``story-4b5265a8``, a fix story the PO created
itself, reached a user who never ordered it. A story is ordered when a confirmed
Product Brief is bound to it; the PO consumer decides that once, before the PO
graph, for every producer's story event.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
from langchain_core.messages import AIMessage
import pytest
from structlog.testing import capture_logs

from shared.contracts.queues.po import POReminderMessage, POSystemEvent, POUserMessage
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_CONSUMER_GROUP, PO_INPUT_QUEUE
from src.clients.api import LanggraphAPIClient
from src.consumers import po as po_consumer
from src.consumers.po import _handle_message, _process_message

CHAT = "1015926438"

#: The watchdog's owner text for a pull request that never merged
#: (``scheduler/src/tasks/supervisor/state_age.py``), as it reached the user.
PR_REVIEW_OWNER_TEXT = (
    "The finished pull request for this change has not moved for over 120 minutes and "
    "was never merged, so nothing is being deployed. A specialist has to look at this; "
    "nothing more happens automatically."
)


@pytest.fixture
def graph():
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="Work on your bot is stopped.")]}
    state = AsyncMock()
    state.values = {"messages": []}
    graph.aget_state.return_value = state
    return graph


@pytest.fixture
def client():
    client = AsyncMock()
    client.redis = AsyncMock()
    client.publish_flat = AsyncMock()
    return client


@pytest.fixture
def admins(monkeypatch) -> AsyncMock:
    notify = AsyncMock()
    monkeypatch.setattr(po_consumer, "notify_admins_best_effort", notify)
    return notify


def _event(event: OwnerNotificationEvent, story_id: str, text: str, **fields) -> dict:
    return POSystemEvent(
        event=event,
        text=text,
        story_id=story_id,
        project_id="project-1",
        telegram_chat_id=CHAT,
        owner_user_id="user-7",
        **fields,
    ).model_dump(mode="json")


def _published_streams(client) -> list[str]:
    return [call.args[0] for call in client.publish_flat.call_args_list]


class TestTheIncident:
    @pytest.mark.asyncio
    async def test_a_blocked_story_without_a_brief_is_told_to_the_admins_only(
        self, graph, client, admins, ordered_stories
    ):
        ordered_stories.unordered.add("story-4b5265a8")

        await _handle_message(
            graph,
            client,
            CHAT,
            _event(OwnerNotificationEvent.STORY_BLOCKED, "story-4b5265a8", PR_REVIEW_OWNER_TEXT),
        )

        graph.ainvoke.assert_not_called()
        assert "po:proactive" not in _published_streams(client)
        admins.assert_awaited_once()
        text = admins.await_args.args[0]
        assert "Withheld from the user" in text
        assert "not an ordered story" in text
        assert "event=story_blocked" in text
        assert "story=story-4b5265a8" in text
        assert "project=project-1" in text
        assert PR_REVIEW_OWNER_TEXT in text
        assert admins.await_args.kwargs["story_id"] == "story-4b5265a8"

    @pytest.mark.asyncio
    async def test_the_same_event_for_an_ordered_story_is_told_to_the_user(
        self, graph, client, admins, ordered_stories
    ):
        await _handle_message(
            graph,
            client,
            CHAT,
            _event(OwnerNotificationEvent.STORY_BLOCKED, "story-order", PR_REVIEW_OWNER_TEXT),
        )

        graph.ainvoke.assert_called_once()
        content = graph.ainvoke.call_args.args[0]["messages"][0].content
        assert "system_event:story_blocked" in content
        assert PR_REVIEW_OWNER_TEXT in content
        delivered = client.publish_flat.call_args
        assert delivered.args[0] == "po:proactive"
        assert delivered.args[1]["story_id"] == "story-order"
        admins.assert_not_called()
        assert ordered_stories.reads == ["story-order"]

    @pytest.mark.asyncio
    async def test_a_secret_request_reaches_the_user_even_without_a_brief(
        self, graph, client, admins, ordered_stories
    ):
        """Only the user can supply the secret the deployment waits for."""
        ordered_stories.unordered.add("story-tech")

        await _handle_message(
            graph,
            client,
            CHAT,
            _event(
                OwnerNotificationEvent.STORY_WAITING_USER_SECRET,
                "story-tech",
                "Deployment waits for OPENROUTER_API_KEY.",
            ),
        )

        graph.ainvoke.assert_called_once()
        admins.assert_not_called()
        assert ordered_stories.reads == []


class TestTheAudienceRule:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event",
        [
            OwnerNotificationEvent.STORY_COMPLETED,
            OwnerNotificationEvent.STORY_FAILED,
            OwnerNotificationEvent.STORY_QUARANTINED,
            OwnerNotificationEvent.TASK_WAITING_RESOURCES,
            OwnerNotificationEvent.STORY_REQUIREMENTS_RETURNED,
        ],
    )
    async def test_every_story_event_of_a_story_without_a_brief_goes_to_the_admins(
        self, graph, client, admins, ordered_stories, event
    ):
        ordered_stories.unordered.add("story-tech")

        await _handle_message(graph, client, CHAT, _event(event, "story-tech", "Something."))

        graph.ainvoke.assert_not_called()
        client.publish_flat.assert_not_called()
        admins.assert_awaited_once()
        assert f"event={event.value}" in admins.await_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unconfirmed_brief_does_not_make_a_story_ordered(
        self, graph, client, admins, ordered_stories
    ):
        ordered_stories.unconfirmed.add("story-draft")

        await _handle_message(
            graph,
            client,
            CHAT,
            _event(OwnerNotificationEvent.STORY_FAILED, "story-draft", "Planning failed."),
        )

        graph.ainvoke.assert_not_called()
        admins.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_stage_notice_of_a_story_without_a_brief_is_dropped_not_sent_to_admins(
        self, graph, client, admins, ordered_stories
    ):
        ordered_stories.unordered.add("story-tech")

        await _handle_message(
            graph,
            client,
            CHAT,
            _event(
                OwnerNotificationEvent.STORY_STAGE,
                "story-tech",
                "The story is being built.",
                stage="in_progress",
                waiting_on="none",
                wait_estimate="minutes",
                stage_notice="entered",
                stage_notice_step=0,
                stage_entered_at="2026-09-27T10:00:00+00:00",
            ),
        )

        graph.ainvoke.assert_not_called()
        client.publish_flat.assert_not_called()
        admins.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_event_that_names_no_story_is_not_checked(
        self, graph, client, admins, ordered_stories
    ):
        await _handle_message(
            graph,
            client,
            CHAT,
            POSystemEvent(
                event=OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
                text="Resources are back.",
                telegram_chat_id=CHAT,
            ).model_dump(mode="json"),
        )

        graph.ainvoke.assert_called_once()
        assert ordered_stories.reads == []

    @pytest.mark.asyncio
    async def test_the_users_own_message_is_not_checked(
        self, graph, client, admins, ordered_stories
    ):
        await _handle_message(
            graph,
            client,
            CHAT,
            POUserMessage(text="hi", telegram_chat_id=CHAT, request_id="req-1").model_dump(
                mode="json"
            ),
        )

        assert graph.ainvoke.call_count == 1
        assert ordered_stories.reads == []


class TestAReminderNamingAStory:
    """Review of 1405: the audience rule holds at the same entry for a story reminder."""

    @pytest.mark.asyncio
    async def test_a_reminder_about_a_story_nobody_ordered_runs_no_turn(
        self, graph, client, admins, ordered_stories
    ):
        ordered_stories.unordered.add("story-tech")

        with capture_logs() as logs:
            await _handle_message(graph, client, CHAT, _reminder("story-tech"))

        graph.ainvoke.assert_not_called()
        client.publish_flat.assert_not_called()
        admins.assert_not_called()
        assert ordered_stories.reads == ["story-tech"]
        [dropped] = [log for log in logs if log["event"] == "po_unordered_story_reminder_dropped"]
        assert dropped["story_id"] == "story-tech"

    @pytest.mark.asyncio
    async def test_a_reminder_about_an_ordered_story_runs_its_turn(
        self, graph, client, admins, ordered_stories
    ):
        await _handle_message(graph, client, CHAT, _reminder("story-order"))

        graph.ainvoke.assert_called_once()
        assert ordered_stories.reads == ["story-order"]

    @pytest.mark.asyncio
    async def test_an_unanswered_check_leaves_the_reminder_pending(
        self, graph, client, admins, ordered_stories
    ):
        ordered_stories.failing["story-x"] = RuntimeError("API unavailable")

        await _process_message(
            graph,
            client,
            _semaphore(),
            {},
            "1-0",
            POReminderMessage(text="re-check", telegram_chat_id=CHAT, story_id="story-x"),
        )

        client.redis.xack.assert_not_called()
        graph.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reminder_about_no_story_is_not_checked(
        self, graph, client, admins, ordered_stories
    ):
        await _handle_message(graph, client, CHAT, _reminder(""))

        graph.ainvoke.assert_called_once()
        assert ordered_stories.reads == []


def _reminder(story_id: str) -> dict:
    return POReminderMessage(text="re-check", telegram_chat_id=CHAT, story_id=story_id).model_dump(
        mode="json"
    )


class TestUnknownIsNotNotOrdered:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            RuntimeError("arbitrary brief read failure"),
            httpx.ConnectTimeout("api timed out"),
            httpx.HTTPStatusError(
                "server error",
                request=httpx.Request("GET", "http://api/product-briefs/by-story/story-x"),
                response=httpx.Response(503),
            ),
        ],
    )
    async def test_an_unanswered_check_leaves_the_entry_pending(
        self, graph, client, admins, ordered_stories, error
    ):
        """Not acked, so the PEL sweep hands it back; nothing is told to anyone."""
        ordered_stories.failing["story-x"] = error
        message = POSystemEvent(
            event=OwnerNotificationEvent.STORY_BLOCKED,
            text=PR_REVIEW_OWNER_TEXT,
            story_id="story-x",
            telegram_chat_id=CHAT,
        )

        await _process_message(graph, client, _semaphore(), {}, "1-0", message)

        client.redis.xack.assert_not_called()
        graph.ainvoke.assert_not_called()
        client.publish_flat.assert_not_called()
        admins.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(b"\xff", id="invalid-utf8"),
            pytest.param(b'{"id": "brief-1", "project_id', id="invalid-json"),
            pytest.param(b'{"id": "brief-1", "story_id": "story-x"}', id="not-a-brief"),
        ],
    )
    async def test_a_malformed_2xx_brief_leaves_the_entry_pending(
        self, graph, client, admins, monkeypatch, body
    ):
        """A 200 the client cannot read as a brief answers nothing, as an error does."""
        api = po_consumer.api_client
        monkeypatch.setattr(
            api,
            "get_product_brief_by_story",
            LanggraphAPIClient.get_product_brief_by_story.__get__(api),
        )
        request = httpx.Request("GET", "http://api/product-briefs/by-story/story-x")
        monkeypatch.setattr(
            api,
            "request",
            AsyncMock(return_value=httpx.Response(200, content=body, request=request)),
        )
        message = POSystemEvent(
            event=OwnerNotificationEvent.STORY_BLOCKED,
            text=PR_REVIEW_OWNER_TEXT,
            story_id="story-x",
            telegram_chat_id=CHAT,
        )

        await _process_message(graph, client, _semaphore(), {}, "1-0", message)

        api.request.assert_awaited_once()
        client.redis.xack.assert_not_called()
        graph.ainvoke.assert_not_called()
        client.publish_flat.assert_not_called()
        admins.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_answered_check_acks_the_entry(self, graph, client, admins, ordered_stories):
        ordered_stories.unordered.add("story-x")
        message = POSystemEvent(
            event=OwnerNotificationEvent.STORY_BLOCKED,
            text=PR_REVIEW_OWNER_TEXT,
            story_id="story-x",
            telegram_chat_id=CHAT,
        )

        await _process_message(graph, client, _semaphore(), {}, "1-0", message)

        client.redis.xack.assert_awaited_once_with(PO_INPUT_QUEUE, PO_CONSUMER_GROUP, "1-0")
        admins.assert_awaited_once()


def _semaphore() -> asyncio.Semaphore:
    return asyncio.Semaphore(1)
