"""The situation snapshot: what a system event turn knows is true now.

Built by ``agents.po.situation`` from the internal API (``SituationApi``: the real
reader and client over a stub transport) and the chat's last-message record in
Redis (fakeredis). Consumer tests drive ``_handle_message`` and read the snapshot
the graph was invoked with, from its run config.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from fakeredis.aioredis import FakeRedis
from langchain_core.messages import AIMessage
import pytest

from shared.contracts.queues.po import POSystemEvent, POUserMessage
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_PROACTIVE_QUEUE
from src.agents.po import situation as situation_module, tools_shared
from src.agents.po.situation import (
    DEFERRED_NOTICES_HEADING,
    SITUATION_CONFIG_KEY,
    SNAPSHOT_HEADING,
    ApiSituationReader,
    SituationSubject,
    build_situation,
    human_age,
    last_user_message_key,
    when,
)
from src.agents.po.tools_stories import get_product_situation
from src.consumers.po import _handle_message
from tests.unit.factories import make_product_brief
from tests.unit.po.situation_api import (
    MALFORMED,
    NOT_FOUND,
    RAISE,
    application_body,
    project_body,
    repository_body,
)

CHAT = "1015926438"
PROJECT = "00000000-0000-0000-0000-000000000001"
OTHER_PROJECT = "00000000-0000-0000-0000-000000000002"
STORY = "story-order"


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
    client.redis = FakeRedis(decode_responses=True)
    client.publish_flat = AsyncMock()
    return client


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _event(event=OwnerNotificationEvent.STORY_BLOCKED, story_id: str = STORY) -> dict:
    return POSystemEvent(
        event=event,
        text="Work on the story is stopped; a person has to resolve it.",
        story_id=story_id,
        project_id=PROJECT,
        telegram_chat_id=CHAT,
        owner_user_id="user-7",
    ).model_dump(mode="json")


def _snapshot_of(graph) -> str:
    return graph.ainvoke.call_args.kwargs["config"]["configurable"][SITUATION_CONFIG_KEY]


def _line(snapshot: str, label: str) -> str:
    [line] = [line for line in snapshot.splitlines() if line.startswith(f"- {label}:")]
    return line.removeprefix(f"- {label}:").strip()


def _whole_world(situation_api, gate_stories, now: datetime) -> None:
    """Every source answers: two projects, an application, orders and platform work."""
    situation_api.projects = [
        project_body(PROJECT, "Finance bot"),
        project_body(OTHER_PROJECT, "Quiz bot"),
    ]
    situation_api.project_story_ids = {
        PROJECT: [STORY, "story-tech", "story-done"],
        OTHER_PROJECT: ["story-quiz"],
    }
    gate_stories.put(
        STORY,
        title="Expense tracking",
        status="waiting_human_review",
        waiting_on="human_review",
        status_entered_at=_iso(now - timedelta(days=21, hours=2)),
        # Written today without a transition: it must not move the entry age.
        updated_at=_iso(now - timedelta(minutes=5)),
    )
    gate_stories.put("story-tech", title="Upgrade kit", type="technical")
    gate_stories.put("story-done", title="First version", status="completed")
    gate_stories.put(
        "story-quiz",
        project_id=OTHER_PROJECT,
        title="Weekly quiz",
        status_entered_at=_iso(now - timedelta(hours=5)),
    )
    situation_api.briefs[STORY] = make_product_brief(
        story_id=STORY, confirmed_at=now - timedelta(days=24)
    ).model_dump(mode="json")
    situation_api.repositories = {PROJECT: [repository_body(PROJECT)]}
    situation_api.applications = {
        "repo-1": [application_body(last_health_check=_iso(now - timedelta(minutes=4)))]
    }


class TestContent:
    async def test_the_snapshot_states_every_field_with_dates_and_ages(
        self, situation_api, gate_stories, client
    ):
        now = datetime.now(UTC)
        _whole_world(situation_api, gate_stories, now)
        await client.redis.set(last_user_message_key(CHAT), _iso(now - timedelta(days=10)))

        snapshot = await build_situation(
            ApiSituationReader(situation_api.client),
            client.redis,
            SituationSubject(telegram_chat_id=CHAT, project_id=PROJECT, story_id=STORY),
            now=now,
        )

        assert snapshot.startswith(f"{SNAPSHOT_HEADING} (built for this system event, ")
        assert _line(snapshot, "Story") == 'story-order "Expense tracking"'
        assert _line(snapshot, "Order") == (
            f"ordered, Product Brief confirmed {when(now - timedelta(days=24), now)}"
        )
        assert when(now - timedelta(days=24), now).endswith("UTC (3 weeks ago)")
        assert _line(snapshot, "Status") == "waiting_human_review, waiting on human_review"
        assert _line(snapshot, "In this status since") == when(
            now - timedelta(days=21, hours=2), now
        )
        assert _line(snapshot, "User's last message in this chat") == when(
            now - timedelta(days=10), now
        )
        assert _line(snapshot, "Application") == (
            f"finance-bot: running (up), last health check {when(now - timedelta(minutes=4), now)}"
        )
        assert "- Other ordered stories in work: \n" in snapshot
        assert (
            '  - story-quiz "Weekly quiz" (project Quiz bot): in_progress, in it since '
            f"{when(now - timedelta(hours=5), now)}"
        ) in snapshot
        assert 'story-order "Expense tracking" (project' not in snapshot
        assert _line(snapshot, "Platform work in this project") == (
            "1 story in work (not ordered, or technical)"
        )
        assert snapshot.endswith(f"{DEFERRED_NOTICES_HEADING}\nnone")
        # The projects are the chat's own: owner-scoped, read as this Telegram user.
        [(_, _, query)] = [r for r in situation_api.requests if r[0] == "projects"]
        assert query == {"owner_only": "true"}

    async def test_the_quiet_answers_are_words_not_unknown(
        self, situation_api, ordered_stories, client
    ):
        ordered_stories.unordered.add("story-tech")

        snapshot = await build_situation(
            ApiSituationReader(situation_api.client),
            client.redis,
            SituationSubject(telegram_chat_id=CHAT, project_id=PROJECT, story_id="story-tech"),
        )

        assert _line(snapshot, "Order") == "not an order (no confirmed Product Brief)"
        assert _line(snapshot, "User's last message in this chat") == "none recorded"
        assert _line(snapshot, "Application") == "not deployed"
        assert _line(snapshot, "Other ordered stories in work") == "none"
        assert _line(snapshot, "Platform work in this project") == (
            "0 stories in work (not ordered, or technical)"
        )

    async def test_metadata_written_after_the_status_leaves_the_entry_age(
        self, situation_api, gate_stories, client
    ):
        now = datetime.now(UTC)
        entered = now - timedelta(days=21)
        gate_stories.put(
            STORY,
            status="waiting_human_review",
            waiting_on="human_review",
            status_entered_at=_iso(entered),
            title="Renamed today",
            quarantine_reason={"operator_note": "written today"},
            updated_at=_iso(now - timedelta(seconds=30)),
        )

        snapshot = await build_situation(
            ApiSituationReader(situation_api.client),
            client.redis,
            SituationSubject(telegram_chat_id=CHAT, project_id=PROJECT, story_id=STORY),
            now=now,
        )

        assert _line(snapshot, "Story") == 'story-order "Renamed today"'
        assert _line(snapshot, "In this status since") == when(entered, now)
        assert when(entered, now).endswith("(3 weeks ago)")

    async def test_a_story_landed_before_the_entry_time_was_recorded_is_unknown(
        self, situation_api, gate_stories, client
    ):
        """No fallback to ``updated_at``: unrelated writes move it."""
        now = datetime.now(UTC)
        situation_api.projects = [project_body(PROJECT)]
        situation_api.project_story_ids = {PROJECT: [STORY, "story-other"]}
        gate_stories.put(STORY, status_entered_at=None, updated_at=_iso(now))
        gate_stories.put("story-other", status_entered_at=None)

        snapshot = await build_situation(
            ApiSituationReader(situation_api.client),
            client.redis,
            SituationSubject(telegram_chat_id=CHAT, project_id=PROJECT, story_id=STORY),
            now=now,
        )

        assert _line(snapshot, "Status") == "in_progress"
        assert _line(snapshot, "In this status since") == "unknown"
        assert "in_progress, in it since unknown" in snapshot

    async def test_a_watchdog_park_names_the_wait_it_ended(
        self, situation_api, gate_stories, client
    ):
        """A fresh park after three weeks in pr_review: the park is new, the wait is not."""
        now = datetime.now(UTC)
        began = now - timedelta(days=21)
        gate_stories.put(
            STORY,
            status="waiting_human_review",
            waiting_on="human_review",
            status_entered_at=_iso(now - timedelta(minutes=3)),
            quarantine_reason={
                "reason": "state_wait_age_bound_exceeded",
                "status": "pr_review",
                "waiting_on": "ci",
                "config_key": "supervisor.state_age_pr_review_minutes",
                "threshold_minutes": 120,
                "anchor": "github_pull_request_updated_at",
                "anchor_at": _iso(began),
                "age_minutes": 21 * 24 * 60.0,
                "ending": "park",
            },
        )

        snapshot = await build_situation(
            ApiSituationReader(situation_api.client),
            client.redis,
            SituationSubject(telegram_chat_id=CHAT, project_id=PROJECT, story_id=STORY),
            now=now,
        )

        assert _line(snapshot, "Status") == (
            "waiting_human_review, waiting on human_review; stopped after waiting 3 weeks in "
            f"pr_review (that wait began {began:%Y-%m-%d %H:%M} UTC)"
        )
        assert _line(snapshot, "In this status since").endswith("UTC (3 minutes ago)")

    async def test_an_event_naming_no_story_says_so(self, situation_api, client):
        snapshot = await build_situation(
            ApiSituationReader(situation_api.client),
            client.redis,
            SituationSubject(telegram_chat_id=CHAT, project_id=PROJECT),
        )

        assert _line(snapshot, "Story") == "none named by this event"
        assert "- Order:" not in snapshot

    async def test_an_application_that_is_down_is_not_up(self, situation_api, client):
        situation_api.repositories = {PROJECT: [repository_body(PROJECT)]}
        situation_api.applications = {"repo-1": [application_body(status="down")]}

        snapshot = await build_situation(
            ApiSituationReader(situation_api.client),
            client.redis,
            SituationSubject(telegram_chat_id=CHAT, project_id=PROJECT),
        )

        assert _line(snapshot, "Application") == (
            "finance-bot: down (not up), no health check recorded"
        )


@pytest.mark.parametrize(
    ("age", "words"),
    [
        (timedelta(seconds=20), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(minutes=59), "59 minutes ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=13), "13 days ago"),
        (timedelta(days=21), "3 weeks ago"),
        (timedelta(days=62), "8 weeks ago"),
        (timedelta(days=100), "3 months ago"),
        (timedelta(days=800), "2 years ago"),
    ],
)
def test_human_age(age, words):
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert human_age(now - age, now) == words


def test_an_absolute_date_is_utc_with_its_age():
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert when(datetime(2026, 9, 6, 10, 0), now) == "2026-09-06 10:00 UTC (3 weeks ago)"


class TestTotality:
    """A source that fails makes its field unknown; the event still reaches the PO."""

    @pytest.mark.parametrize(
        ("route", "fault", "unknown"),
        [
            *[
                ("story", fault, ["Story", "Status", "In this status since"])
                for fault in (RAISE, NOT_FOUND, MALFORMED)
            ],
            *[("brief", fault, ["Order"]) for fault in (RAISE, MALFORMED)],
            *[
                ("projects", fault, ["Other ordered stories in work"])
                for fault in (RAISE, NOT_FOUND, MALFORMED)
            ],
            *[
                (
                    "project_stories",
                    fault,
                    ["Other ordered stories in work", "Platform work in this project"],
                )
                for fault in (RAISE, NOT_FOUND, MALFORMED)
            ],
            *[("repositories", fault, ["Application"]) for fault in (RAISE, NOT_FOUND, MALFORMED)],
            *[("applications", fault, ["Application"]) for fault in (RAISE, NOT_FOUND, MALFORMED)],
        ],
    )
    async def test_a_failing_api_source_is_unknown_and_the_turn_runs(
        self, graph, client, situation_api, gate_stories, route, fault, unknown
    ):
        now = datetime.now(UTC)
        _whole_world(situation_api, gate_stories, now)
        await client.redis.set(last_user_message_key(CHAT), _iso(now - timedelta(days=2)))
        situation_api.faults[route] = fault

        await _handle_message(graph, client, CHAT, _event())

        graph.ainvoke.assert_awaited_once()
        assert client.publish_flat.await_args.args[0] == PO_PROACTIVE_QUEUE
        snapshot = _snapshot_of(graph)
        for label in unknown:
            assert _line(snapshot, label) == "unknown", label
        assert _line(snapshot, "User's last message in this chat").endswith("UTC (2 days ago)")
        assert snapshot.endswith(f"{DEFERRED_NOTICES_HEADING}\nnone")

    async def test_a_brief_404_is_an_answer_not_a_failure(self, graph, client, situation_api):
        """The audience check passed on its own read; the snapshot's read says no brief."""
        situation_api.faults["brief"] = NOT_FOUND

        await _handle_message(graph, client, CHAT, _event())

        assert _line(_snapshot_of(graph), "Order") == "not an order (no confirmed Product Brief)"

    @pytest.mark.parametrize("stored", ["yesterday-ish", 42])
    async def test_an_unreadable_last_message_record_is_unknown(self, graph, client, stored):
        await client.redis.set(last_user_message_key(CHAT), stored)

        await _handle_message(graph, client, CHAT, _event())

        graph.ainvoke.assert_awaited_once()
        assert _line(_snapshot_of(graph), "User's last message in this chat") == "unknown"

    async def test_an_unreachable_redis_is_unknown(self, graph, client):
        client.redis = AsyncMock()
        client.redis.get.side_effect = ConnectionError("redis gone")

        await _handle_message(graph, client, CHAT, _event())

        graph.ainvoke.assert_awaited_once()
        assert _line(_snapshot_of(graph), "User's last message in this chat") == "unknown"

    async def test_a_failing_deferred_notice_source_is_unknown(self, graph, client, monkeypatch):
        monkeypatch.setattr(
            situation_module, "read_deferred_notices", AsyncMock(side_effect=RuntimeError("x"))
        )

        await _handle_message(graph, client, CHAT, _event())

        assert _snapshot_of(graph).endswith(f"{DEFERRED_NOTICES_HEADING}\nunknown")

    async def test_everything_failing_still_hands_the_event_to_the_po(
        self, graph, client, situation_api
    ):
        for route in (
            "story",
            "brief",
            "projects",
            "project_stories",
            "repositories",
            "applications",
        ):
            situation_api.faults[route] = RAISE

        await _handle_message(graph, client, CHAT, _event())

        content = graph.ainvoke.call_args.args[0]["messages"][0].content
        assert "system_event:story_blocked" in content
        assert SNAPSHOT_HEADING in _snapshot_of(graph)

    async def test_the_audience_check_stays_fail_closed(
        self, graph, client, ordered_stories, situation_api
    ):
        """1404's rule is not the snapshot's: an unknown audience is not handed over."""
        from src.consumers.po import StoryAudienceUnknown

        ordered_stories.failing[STORY] = RuntimeError("brief read failed")

        with pytest.raises(StoryAudienceUnknown):
            await _handle_message(graph, client, CHAT, _event())

        graph.ainvoke.assert_not_called()
        assert situation_api.requests == []


class TestAcceptance:
    """DoD8: an old blocked order is told by its dates; a status question has no snapshot."""

    async def test_a_three_week_old_block_reaches_the_graph_with_its_dates(
        self, graph, client, gate_stories, situation_api
    ):
        now = datetime.now(UTC)
        ordered = now - timedelta(days=23)
        blocked = now - timedelta(days=21)
        gate_stories.put(
            STORY,
            title="Expense tracking",
            status="waiting_human_review",
            waiting_on="human_review",
            status_entered_at=_iso(blocked),
            # A title edit and quarantine metadata written today, without a transition.
            updated_at=_iso(now - timedelta(minutes=1)),
            quarantine_reason={"qa_failure": {"summary": "noted today"}},
        )
        situation_api.briefs[STORY] = make_product_brief(
            story_id=STORY, confirmed_at=ordered
        ).model_dump(mode="json")

        await _handle_message(graph, client, CHAT, _event())

        snapshot = _snapshot_of(graph)
        order = _line(snapshot, "Order")
        assert f"{ordered:%Y-%m-%d %H:%M} UTC (3 weeks ago)" in order
        assert _line(snapshot, "Status") == "waiting_human_review, waiting on human_review"
        assert _line(snapshot, "In this status since") == (
            f"{blocked:%Y-%m-%d %H:%M} UTC (3 weeks ago)"
        )
        # The event itself stays one line in the chat; the snapshot is not in it.
        content = graph.ainvoke.call_args.args[0]["messages"][0].content
        assert SNAPSHOT_HEADING not in content

    async def test_a_status_question_in_a_user_turn_carries_no_snapshot(
        self, graph, client, situation_api
    ):
        await _handle_message(
            graph,
            client,
            CHAT,
            POUserMessage(
                text="How is my bot going?", telegram_chat_id=CHAT, request_id="req-1"
            ).model_dump(mode="json"),
        )

        configurable = graph.ainvoke.call_args.kwargs["config"]["configurable"]
        assert SITUATION_CONFIG_KEY not in configurable
        assert situation_api.requests == []


class TestLastUserMessage:
    async def test_a_user_turn_records_when_the_user_wrote(self, graph, client):
        before = datetime.now(UTC)
        await _handle_message(
            graph,
            client,
            CHAT,
            POUserMessage(text="hi", telegram_chat_id=CHAT, request_id="req-1").model_dump(
                mode="json"
            ),
        )

        recorded = datetime.fromisoformat(await client.redis.get(last_user_message_key(CHAT)))
        assert before <= recorded <= datetime.now(UTC)

        await _handle_message(graph, client, CHAT, _event())
        assert _line(_snapshot_of(graph), "User's last message in this chat").endswith(
            "UTC (just now)"
        )

    async def test_a_failed_record_does_not_fail_the_users_turn(self, graph, client):
        client.redis = AsyncMock()
        client.redis.set.side_effect = ConnectionError("redis gone")

        await _handle_message(
            graph,
            client,
            CHAT,
            POUserMessage(text="hi", telegram_chat_id=CHAT, request_id="req-1").model_dump(
                mode="json"
            ),
        )

        graph.ainvoke.assert_awaited_once()
        assert client.publish_flat.await_args.args[0] == "po:response:req-1"


class TestGetProductSituation:
    @pytest.fixture
    def stream(self, monkeypatch, situation_api):
        stream = AsyncMock()
        stream.redis = FakeRedis(decode_responses=True)
        stream.publish_flat = AsyncMock()
        monkeypatch.setattr(tools_shared, "_api_client", situation_api.client)
        monkeypatch.setattr(tools_shared, "_stream_client", stream)
        return stream

    @staticmethod
    def _config(user_turn: bool = True) -> dict:
        return {"configurable": {"telegram_chat_id": CHAT, "user_turn": user_turn}}

    async def test_answers_the_snapshot_for_the_current_ordered_story(
        self, stream, situation_api, gate_stories
    ):
        now = datetime.now(UTC)
        _whole_world(situation_api, gate_stories, now)

        answer = await get_product_situation.ainvoke({"project_id": PROJECT}, config=self._config())

        assert answer.startswith(f"{SNAPSHOT_HEADING} (requested, ")
        assert _line(answer, "Story") == 'story-order "Expense tracking"'
        assert "3 weeks ago" in _line(answer, "Order")
        assert _line(answer, "Status").startswith("waiting_human_review")
        assert f"{DEFERRED_NOTICES_HEADING}\nnone" in answer
        stream.publish_flat.assert_not_called()

    async def test_the_latest_ordered_story_when_none_is_in_work(
        self, stream, situation_api, gate_stories, ordered_stories
    ):
        situation_api.projects = [project_body(PROJECT)]
        situation_api.project_story_ids = {PROJECT: ["story-old", "story-new", "story-tech"]}
        gate_stories.put("story-old", status="completed", created_at="2026-08-01T10:00:00+00:00")
        gate_stories.put("story-new", status="completed", created_at="2026-09-01T10:00:00+00:00")
        gate_stories.put("story-tech", created_at="2026-09-20T10:00:00+00:00")
        ordered_stories.unordered.add("story-tech")

        answer = await get_product_situation.ainvoke({"project_id": PROJECT}, config=self._config())

        assert _line(answer, "Story").startswith("story-new ")
        assert _line(answer, "Status").startswith("completed")

    async def test_a_project_with_no_order_says_so(self, stream, situation_api, ordered_stories):
        situation_api.projects = [project_body(PROJECT)]

        answer = await get_product_situation.ainvoke({"project_id": PROJECT}, config=self._config())

        assert _line(answer, "Story") == "no ordered story in this project"

    async def test_someone_elses_project_is_not_read(self, stream, situation_api):
        situation_api.projects = [project_body(OTHER_PROJECT)]

        answer = await get_product_situation.ainvoke({"project_id": PROJECT}, config=self._config())

        assert answer == f"No project {PROJECT} among this user's projects."
        assert [route for route, _, _ in situation_api.requests] == ["projects"]
        stream.publish_flat.assert_not_called()
