"""A proactive PO message about a story says a change or a new escalation step, never a repeat.

The PO turn runs through ``_handle_message`` with a stub graph that always has
something to say — the model is not trusted to stay quiet — and the gate is the
real one over fakeredis, reading stories from a stub API. What reached the user
is what was published to ``po:proactive``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage
import pytest
from structlog.testing import capture_logs

from shared.contracts.dto.story import (
    WAITING_ON_BY_STATUS,
    StoryStageNoticeKind,
    StoryStatus,
    StoryWaitEstimate,
)
from shared.contracts.queues.po import POReminderMessage, POSystemEvent
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_PROACTIVE_QUEUE
from src.consumers import po as po_consumer
from src.consumers.po import _handle_message
from src.consumers.po_story_gate import ProactiveStoryGate, story_told_key

CHAT = "1015926438"
STORY = "story-stuck"
T0 = datetime(2026, 9, 26, 6, 0, tzinfo=UTC)
FAILURE = {
    "reason": "story_failure",
    "code": "planning_failed",
    "source": "architect",
    "detail": "LLMChannelsExhausted",
    "observed_at": "2026-09-26T10:00:00+00:00",
}


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def at(self, minutes: float) -> None:
        self.now = T0 + timedelta(minutes=minutes)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def cap() -> dict[str, int]:
    return {"value": 6}


@pytest.fixture(autouse=True)
def gate(monkeypatch, gate_redis, gate_stories, clock, cap) -> ProactiveStoryGate:
    gate = ProactiveStoryGate(
        gate_redis, gate_stories, lambda: cap["value"], clock=lambda: clock.now
    )
    monkeypatch.setattr(po_consumer, "_story_gate", gate)
    return gate


@pytest.fixture
def graph():
    """A PO that always writes a reply: 'work is going'."""
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content="Work on it is going.")]}
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


def _told(client) -> list[dict]:
    """Every message that reached the user's chat."""
    return [
        call.args[1]
        for call in client.publish_flat.await_args_list
        if call.args[0] == PO_PROACTIVE_QUEUE
    ]


def _reminder(story_id: str = STORY) -> dict:
    return POReminderMessage(
        text=f"re-check story {story_id}", telegram_chat_id=CHAT, story_id=story_id
    ).model_dump(mode="json")


def _stage_notice(
    step: int, stage: StoryStatus = StoryStatus.IN_PROGRESS, *, entered_at: datetime = T0
) -> dict:
    """Step *step* of the stay in *stage* whose entry notice went out at *entered_at*."""
    return POSystemEvent(
        event=OwnerNotificationEvent.STORY_STAGE,
        text=f"Story is at stage {stage.value}.",
        telegram_chat_id=CHAT,
        story_id=STORY,
        project_id="project-1",
        stage=stage,
        waiting_on=WAITING_ON_BY_STATUS[stage],
        wait_estimate=StoryWaitEstimate.UNBOUNDED,
        stage_notice=StoryStageNoticeKind.ENTERED
        if step == 0
        else StoryStageNoticeKind.STILL_THERE,
        stage_notice_step=step,
        stage_entered_at=entered_at,
    ).model_dump(mode="json")


def _event(event: OwnerNotificationEvent) -> dict:
    return POSystemEvent(
        event=event, text=str(event), telegram_chat_id=CHAT, story_id=STORY, project_id="p"
    ).model_dump(mode="json")


async def _turn(graph, client, data: dict) -> None:
    await _handle_message(graph, client, CHAT, data)


# ── an unchanged story ───────────────────────────────────────────────────


async def test_an_unchanged_story_is_told_once_per_step_and_never_in_between(graph, client, clock):
    await _turn(graph, client, _stage_notice(0))
    for step in (1, 2, 3):
        for _ in range(5):
            await _turn(graph, client, _reminder())
            await _turn(graph, client, _stage_notice(step - 1))  # a redelivered old step
        await _turn(graph, client, _stage_notice(step))
        await _turn(graph, client, _stage_notice(step))

    assert len(_told(client)) == 4
    # Every turn still ran: the PO keeps its thread, only the message is withheld.
    assert graph.ainvoke.await_count == 1 + 3 * (10 + 2)


async def test_a_redelivered_older_step_is_never_told_again(graph, client):
    """At-least-once `po:input`: steps 0, 1, 2, then late copies of 1 and 2."""
    for step in (0, 1, 2, 1, 2, 0):
        await _turn(graph, client, _stage_notice(step))

    assert len(_told(client)) == 3


async def test_a_return_to_the_stage_is_a_new_stay_whose_steps_are_told(
    graph, client, gate_stories
):
    """In work, parked without the user being told, back in the same stage later.

    The fingerprint is the same as last told, so only the new stay makes its
    steps new; a late copy of the old stay's notice stays suppressed.
    """
    await _turn(graph, client, _stage_notice(0))
    await _turn(graph, client, _stage_notice(1))
    await _turn(graph, client, _stage_notice(2))

    back = T0 + timedelta(hours=3)
    await _turn(graph, client, _stage_notice(0, entered_at=back))
    await _turn(graph, client, _stage_notice(1, entered_at=back))
    await _turn(graph, client, _stage_notice(2))  # the old stay, redelivered
    await _turn(graph, client, _stage_notice(1, entered_at=back))

    assert len(_told(client)) == 5


async def test_a_change_is_told_even_by_a_stale_step_and_the_stay_is_kept(
    graph, client, gate_stories
):
    await _turn(graph, client, _stage_notice(0))
    await _turn(graph, client, _stage_notice(1))
    gate_stories.put(STORY, waiting_on="resources")
    await _turn(graph, client, _stage_notice(0))  # stale step, but the story changed
    await _turn(graph, client, _stage_notice(1))

    assert len(_told(client)) == 3


async def test_a_self_reminder_about_an_unchanged_story_cannot_loop(graph, client):
    for _ in range(14):
        await _turn(graph, client, _reminder())

    assert len(_told(client)) == 1


async def test_a_suppression_is_logged_with_its_reason_and_fingerprint(graph, client):
    await _turn(graph, client, _reminder())
    with capture_logs() as logs:
        await _turn(graph, client, _reminder())

    [suppressed] = [log for log in logs if log["event"] == "po_proactive_suppressed"]
    assert suppressed["story_id"] == STORY
    assert suppressed["reason"] == "unchanged"
    assert suppressed["fingerprint"] == {
        "status": "in_progress",
        "waiting_on": "none",
        "failure_code": None,
        "planning_state": None,
        "planning_failed_attempts": None,
    }


# ── a change is told once ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "change",
    [
        {"status": "pr_review", "waiting_on": "ci"},
        {"waiting_on": "resources"},
        {"status": "waiting_human_review", "quarantine_reason": FAILURE},
        {
            "planning": {
                "state": "retrying",
                "failed_attempts": 1,
                "max_retries": 3,
                "next_attempt_at": "2026-09-26T10:01:00+00:00",
                "last_failure": FAILURE,
                "recorded_at": "2026-09-26T10:00:00+00:00",
            }
        },
    ],
    ids=["status", "waiting_on", "failure", "planning"],
)
async def test_a_change_is_told_once(graph, client, gate_stories, change):
    await _turn(graph, client, _reminder())
    gate_stories.put(STORY, **change)
    for _ in range(4):
        await _turn(graph, client, _reminder())

    assert len(_told(client)) == 2


async def test_another_failed_planning_attempt_is_a_change(graph, client, gate_stories):
    planning = {
        "state": "retrying",
        "max_retries": 3,
        "last_failure": FAILURE,
        "recorded_at": "2026-09-26T10:00:00+00:00",
    }
    for attempts in (1, 1, 2, 2, 3):
        gate_stories.put(STORY, planning={**planning, "failed_attempts": attempts})
        await _turn(graph, client, _reminder())

    assert len(_told(client)) == 3


async def test_a_durable_owner_notice_is_told_and_then_not_repeated(graph, client, gate_stories):
    """A parked story's own notice is not gated; the reminder after it is."""
    await _turn(graph, client, _reminder())
    gate_stories.put(STORY, status="waiting_user_secret", waiting_on="user_secret")
    await _turn(graph, client, _event(OwnerNotificationEvent.STORY_WAITING_USER_SECRET))
    await _turn(graph, client, _reminder())

    assert [told["event"] if "event" in told else "reminder" for told in _told(client)] == [
        "reminder",
        "story_waiting_user_secret",
    ]


# ── a duplicate is allowed, a lost change is not ─────────────────────────


async def test_a_failed_publish_leaves_the_change_to_be_told_again(
    graph, client, gate_stories, gate_redis
):
    await _turn(graph, client, _reminder())
    gate_stories.put(STORY, status="pr_review", waiting_on="ci")
    client.publish_flat.side_effect = ConnectionError("redis gone")
    with pytest.raises(ConnectionError):
        await _turn(graph, client, _reminder())

    client.publish_flat.side_effect = None
    await _turn(graph, client, _reminder())
    await _turn(graph, client, _reminder())

    # One tell before, the failed attempt (recorded by the mock), then the change
    # told again, and only once.
    attempts = [call.args[0] for call in client.publish_flat.await_args_list]
    assert attempts == [PO_PROACTIVE_QUEUE] * 3
    told = await gate_redis.redis.get(story_told_key(CHAT, STORY))
    assert '"status": "pr_review"' in told


async def test_a_gate_that_cannot_read_the_story_lets_the_reply_through(
    graph, client, gate_stories, gate_redis
):
    await _turn(graph, client, _reminder())
    gate_stories.get_story = AsyncMock(side_effect=RuntimeError("api down"))

    await _turn(graph, client, _reminder())
    await _turn(graph, client, _reminder())

    assert len(_told(client)) == 3


# ── the daily backstop ───────────────────────────────────────────────────


async def test_the_daily_cap_holds_even_for_real_changes(graph, client, gate_stories, clock, cap):
    cap["value"] = 2
    statuses = [("in_progress", "none"), ("pr_review", "ci"), ("deploying", "deploy")]
    for status, waiting_on in statuses:
        gate_stories.put(STORY, status=status, waiting_on=waiting_on)
        with capture_logs() as logs:
            await _turn(graph, client, _reminder())

    assert len(_told(client)) == 2
    assert [log["reason"] for log in logs if log["event"] == "po_proactive_suppressed"] == [
        "daily_cap"
    ]

    # The next UTC day the change nobody heard about is told.
    clock.at(24 * 60)
    await _turn(graph, client, _reminder())
    assert len(_told(client)) == 3


async def test_terminal_and_other_durable_notices_are_neither_capped_nor_counted(
    graph, client, gate_stories, cap
):
    cap["value"] = 1
    await _turn(graph, client, _reminder())
    gate_stories.put(STORY, status="waiting_human_review", quarantine_reason=FAILURE)
    await _turn(graph, client, _event(OwnerNotificationEvent.STORY_BLOCKED))
    gate_stories.put(STORY, status="failed", quarantine_reason=FAILURE)
    await _turn(graph, client, _event(OwnerNotificationEvent.STORY_FAILED))

    assert len(_told(client)) == 3


# ── the record ends with the story ───────────────────────────────────────


@pytest.mark.parametrize(
    ("event", "status"),
    [
        (OwnerNotificationEvent.STORY_COMPLETED, "completed"),
        (OwnerNotificationEvent.STORY_FAILED, "failed"),
    ],
)
async def test_the_storys_ending_forgets_the_record(
    graph, client, gate_stories, gate_redis, event, status
):
    await _turn(graph, client, _reminder())
    assert await gate_redis.redis.get(story_told_key(CHAT, STORY)) is not None

    gate_stories.put(STORY, status=status)
    await _turn(graph, client, _event(event))

    assert await gate_redis.redis.get(story_told_key(CHAT, STORY)) is None
    assert len(_told(client)) == 2


async def test_a_reminder_about_an_ended_story_is_not_told_and_forgets_the_record(
    graph, client, gate_stories, gate_redis
):
    """The ending is the durable seam's to tell, e.g. an archived story told nothing here."""
    await _turn(graph, client, _reminder())
    gate_stories.put(STORY, status="archived")

    with capture_logs() as logs:
        await _turn(graph, client, _reminder())

    assert len(_told(client)) == 1
    assert [log["reason"] for log in logs if log["event"] == "po_proactive_suppressed"] == [
        "story_ended"
    ]
    assert await gate_redis.redis.get(story_told_key(CHAT, STORY)) is None


async def test_the_record_is_per_chat_and_expires_unrefreshed(graph, client, gate_redis):
    await _turn(graph, client, _reminder())
    await _handle_message(graph, client, "other-chat", _reminder())

    assert len(_told(client)) == 2
    ttl = await gate_redis.redis.ttl(story_told_key(CHAT, STORY))
    assert 0 < ttl <= 30 * 24 * 3600


# ── what the gate does not touch ─────────────────────────────────────────


async def test_a_turn_without_a_story_is_unaffected(graph, client):
    for _ in range(3):
        await _turn(graph, client, {"type": "reminder", "text": "check the budget"})

    assert len(_told(client)) == 3


async def test_a_user_message_is_answered_as_before(graph, client):
    for index in range(3):
        await _turn(
            graph,
            client,
            {"type": "user_message", "text": "how is it?", "request_id": f"r{index}"},
        )

    assert len(client.publish_flat.await_args_list) == 3
    assert _told(client) == []


# ── notify_user: no second way to the user ───────────────────────────────


@pytest.fixture
def notifying_graph(client, monkeypatch):
    """A PO that calls `notify_user` mid-turn, then writes its final reply."""
    from src.agents.po import tools_shared
    from src.agents.po.tools import notify_user

    monkeypatch.setattr(tools_shared, "_stream_client", client)
    tool_results: list[str] = []

    async def invoke(_input, config):
        tool_results.append(
            await notify_user.ainvoke({"message": "Checking on it..."}, config=config)
        )
        return {"messages": [AIMessage(content="Work on it is going.")]}

    graph = AsyncMock()
    graph.ainvoke.side_effect = invoke
    state = AsyncMock()
    state.values = {"messages": []}
    graph.aget_state.return_value = state
    graph.tool_results = tool_results
    return graph


async def test_notify_user_in_a_reminder_turn_reaches_nobody(notifying_graph, client):
    for _ in range(5):
        await _turn(notifying_graph, client, _reminder())
        await _turn(notifying_graph, client, _stage_notice(0))

    # Only gated final replies — the first reminder's news and the stay's entry
    # step; the tool published nothing at all.
    assert [told["text"] for told in _told(client)] == ["Work on it is going."] * 2
    assert len(notifying_graph.tool_results) == 10
    assert all(result.startswith("Not sent:") for result in notifying_graph.tool_results)


async def test_notify_user_in_a_user_turn_still_sends(notifying_graph, client):
    await _turn(
        notifying_graph, client, {"type": "user_message", "text": "how?", "request_id": "r1"}
    )

    assert [told["text"] for told in _told(client)] == ["Checking on it..."]
    assert notifying_graph.tool_results == ["Message sent to user."]


def test_no_po_tool_but_notify_user_publishes_to_the_proactive_stream():
    """A new direct publisher would bypass the gate; this names it."""
    import ast
    from pathlib import Path

    import src.agents.po as po_package

    publishers = set()
    for path in sorted(Path(po_package.__file__).parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                names_queue = (
                    isinstance(node, ast.Attribute | ast.Name)
                    and getattr(node, "attr", getattr(node, "id", None)) == "PO_PROACTIVE_QUEUE"
                )
                names_stream = isinstance(node, ast.Constant) and node.value == "po:proactive"
                if names_queue or names_stream:
                    publishers.add(f"{path.name}:{function.name}")

    assert publishers == {"tools.py:notify_user"}


# ── the production shape ─────────────────────────────────────────────────


async def test_tg_1015926438_eight_stuck_hours_are_four_messages_not_a_flood(graph, client, clock):
    """An ``in_progress`` story in an unbounded stage for 8 hours.

    The PO re-set a 15-minute reminder every time it fired, and the stage
    notices came at the scheduler's schedule: entry, then steps at 1, 2 and 4
    hours (`test_stage_notices.py::test_tg_1015926438_eight_hours_in_an_unbounded_stage`).
    Before the gate that was at least 14 "work is going" messages, then one an
    hour. Now it is the entry and the three steps.
    """
    stage_notice_at = {0: 0, 60: 1, 120: 2, 240: 3}
    for minute in range(0, 8 * 60, 15):
        clock.at(minute)
        if minute in stage_notice_at:
            await _turn(graph, client, _stage_notice(stage_notice_at[minute]))
        await _turn(graph, client, _reminder())

    assert len(_told(client)) == 4


def test_the_reminder_tool_tells_the_po_a_reminder_is_a_re_check():
    """The prompt is at its length cap; the tool's own text carries the rule."""
    from src.agents.po.tools import set_reminder

    text = " ".join(set_reminder.description.split())
    assert "A reminder is for you to re-check, not a scheduled message to the user." in text
    assert "The user hears about a story only when its state has changed" in text
