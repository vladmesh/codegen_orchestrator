"""A proactive PO message about a story says only an untold key change.

The PO turn runs through ``_handle_message`` with a stub graph that always has
something to say — the model is not trusted to stay quiet — and the gate is the
real one over fakeredis, reading stories from a stub API. What reached the user
is what was published to ``po:proactive``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
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
from src.consumers.po import _handle_message
from src.consumers.po_story_gate import story_told_key

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


async def test_tg_1015926438_progress_is_silent_then_an_untold_stop_is_told_once(
    graph, client, gate_stories
):
    for index in range(14):
        reminder = _reminder()
        reminder["timestamp"] = f"2026-09-26T{6 + index // 4:02}:{index % 4 * 15:02}:00+00:00"
        await _turn(graph, client, reminder)
        if index % 4 == 0:
            await _turn(graph, client, _stage_notice(index // 4))
    assert _told(client) == []
    assert graph.ainvoke.await_count == 14

    gate_stories.put(STORY, status="waiting_human_review", quarantine_reason=FAILURE)
    for _ in range(5):
        await _turn(graph, client, _reminder())
    assert len(_told(client)) == 1


@pytest.mark.parametrize("step", [0, 1, 2, 8])
async def test_stage_notices_are_dropped_before_any_graph_or_audience_read(
    graph, client, ordered_stories, step
):
    ordered_stories.failing[STORY] = RuntimeError("API unavailable")
    with capture_logs() as logs:
        await _turn(graph, client, _stage_notice(step))
    graph.ainvoke.assert_not_called()
    graph.aget_state.assert_not_called()
    assert ordered_stories.reads == []
    assert _told(client) == []
    assert any(log["event"] == "po_story_stage_notice_dropped" for log in logs)


@pytest.mark.parametrize(
    "status", ["created", "in_progress", "reopened", "pr_review", "deploying", "testing"]
)
@pytest.mark.parametrize("waiting_on", ["none", "resources"])
async def test_in_work_is_never_told(graph, client, gate_stories, status, waiting_on):
    gate_stories.put(STORY, status=status, waiting_on=waiting_on)
    with capture_logs() as logs:
        await _turn(graph, client, _reminder())
    assert _told(client) == []
    [suppressed] = [log for log in logs if log["event"] == "po_proactive_suppressed"]
    assert suppressed["reason"] == "in_work"
    assert suppressed["key_state"] == "in_work"


@pytest.mark.parametrize("status", ["waiting_user_secret", "waiting_human_review"])
async def test_a_key_change_is_told_once(graph, client, gate_stories, status):
    gate_stories.put(STORY, status=status)
    for _ in range(4):
        await _turn(graph, client, _reminder())
    assert len(_told(client)) == 1


async def test_planning_failures_are_one_stop_not_new_messages_per_attempt(
    graph, client, gate_stories
):
    for state, attempts in [("retrying", 1), ("retrying", 2), ("parked", 3)]:
        gate_stories.put(
            STORY,
            planning={
                "state": state,
                "failed_attempts": attempts,
                "max_retries": 3,
                "last_failure": FAILURE,
                "recorded_at": T0.isoformat(),
            },
        )
        await _turn(graph, client, _reminder())
    assert len(_told(client)) == 1


async def test_a_planned_story_stays_silent(graph, client, gate_stories):
    gate_stories.put(
        STORY,
        planning={
            "state": "planned",
            "failed_attempts": 0,
            "recorded_at": T0.isoformat(),
        },
    )
    await _turn(graph, client, _reminder())
    assert _told(client) == []


async def test_a_planning_failure_without_a_planning_record_is_a_stop(graph, client, gate_stories):
    gate_stories.put(STORY, quarantine_reason=FAILURE)
    await _turn(graph, client, _reminder())
    await _turn(graph, client, _reminder())
    assert len(_told(client)) == 1


async def test_in_work_does_not_erase_the_stop_last_told(graph, client, gate_stories):
    for status in ("waiting_human_review", "in_progress", "waiting_human_review"):
        gate_stories.put(STORY, status=status)
        await _turn(graph, client, _reminder())
    assert len(_told(client)) == 1


async def test_durable_key_events_still_publish_when_story_read_is_unavailable(
    graph, client, gate_stories
):
    gate_stories.get_story = AsyncMock(side_effect=RuntimeError("api down"))
    await _turn(graph, client, _event(OwnerNotificationEvent.STORY_BLOCKED))
    assert len(_told(client)) == 1


async def test_distinct_key_changes_are_not_capped(graph, client, gate_stories):
    for _ in range(8):
        for status in ("waiting_user_secret", "waiting_human_review"):
            gate_stories.put(STORY, status=status)
            await _turn(graph, client, _reminder())
    assert len(_told(client)) == 16


@pytest.mark.parametrize(
    ("event", "status"),
    [
        (OwnerNotificationEvent.STORY_BLOCKED, "waiting_human_review"),
        (OwnerNotificationEvent.STORY_QUARANTINED, "waiting_human_review"),
        (OwnerNotificationEvent.STORY_IMPOSSIBLE_CAPACITY, "waiting_human_review"),
        (OwnerNotificationEvent.TASK_IMPOSSIBLE_CAPACITY, "waiting_human_review"),
        (OwnerNotificationEvent.STORY_WAITING_USER_SECRET, "waiting_user_secret"),
        (OwnerNotificationEvent.STORY_REQUIREMENTS_RETURNED, "in_progress"),
    ],
)
async def test_a_durable_notice_is_told_and_then_not_repeated(
    graph, client, gate_stories, event, status
):
    gate_stories.put(STORY, status=status)
    await _turn(graph, client, _event(event))
    await _turn(graph, client, _reminder())
    assert [told["event"] for told in _told(client)] == [event.value]


@pytest.mark.parametrize(
    "event",
    [
        OwnerNotificationEvent.TASK_WAITING_RESOURCES,
        OwnerNotificationEvent.TASK_WAITING_INFRASTRUCTURE,
        OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
    ],
)
@pytest.mark.parametrize("with_story", [True, False])
async def test_resource_events_run_the_turn_but_never_publish(graph, client, event, with_story):
    data = _event(event)
    if not with_story:
        del data["story_id"]
    with capture_logs() as logs:
        await _turn(graph, client, data)
    graph.ainvoke.assert_awaited_once()
    assert _told(client) == []
    assert any(
        log["event"] == "po_proactive_suppressed" and log["reason"] == "intermediate_event"
        for log in logs
    )


@pytest.mark.parametrize("planning_state", [None, "retrying", "parked"])
async def test_previous_record_format_does_not_repeat_a_stop(
    graph, client, gate_stories, gate_redis, planning_state
):
    gate_stories.put(STORY, status="waiting_human_review")
    await gate_redis.redis.set(
        story_told_key(CHAT, STORY),
        json.dumps(
            {
                "fingerprint": {
                    "status": "waiting_human_review" if planning_state is None else "in_progress",
                    "waiting_on": "none",
                    "failure_code": "planning_failed",
                    "planning_state": planning_state,
                    "planning_failed_attempts": 1,
                },
                "stay": None,
            }
        ),
        ex=60,
    )
    await _turn(graph, client, _reminder())
    assert _told(client) == []

    assert await gate_redis.redis.ttl(story_told_key(CHAT, STORY)) == -1


async def test_a_failed_publish_leaves_the_change_untold(graph, client, gate_stories, gate_redis):
    gate_stories.put(STORY, status="waiting_human_review")
    client.publish_flat.side_effect = ConnectionError("redis gone")
    with pytest.raises(ConnectionError):
        await _turn(graph, client, _reminder())
    assert await gate_redis.redis.get(story_told_key(CHAT, STORY)) is None
    client.publish_flat.side_effect = None
    await _turn(graph, client, _reminder())
    await _turn(graph, client, _reminder())
    assert len(_told(client)) == 2  # failed attempt, then one successful publish


@pytest.mark.parametrize("boundary", ["api", "redis", "corrupt_record"])
async def test_an_unavailable_gate_never_publishes_progress(
    graph, client, gate_stories, gate_redis, monkeypatch, boundary
):
    if boundary == "api":
        gate_stories.get_story = AsyncMock(side_effect=RuntimeError("api down"))
    else:
        gate_stories.put(STORY, status="waiting_human_review")
        if boundary == "redis":
            monkeypatch.setattr(
                gate_redis.redis, "get", AsyncMock(side_effect=RuntimeError("redis down"))
            )
        else:
            await gate_redis.redis.set(story_told_key(CHAT, STORY), "broken")
    await _turn(graph, client, _reminder())
    assert _told(client) == []


@pytest.mark.parametrize(
    ("event", "status"),
    [
        (OwnerNotificationEvent.STORY_COMPLETED, "completed"),
        (OwnerNotificationEvent.STORY_FAILED, "failed"),
    ],
)
async def test_terminal_notice_publishes_and_forgets_record(
    graph, client, gate_stories, gate_redis, event, status
):
    gate_stories.put(STORY, status="waiting_human_review")
    await _turn(graph, client, _reminder())
    gate_stories.put(STORY, status=status)
    await _turn(graph, client, _event(event))
    await _turn(graph, client, _reminder())
    assert len(_told(client)) == 2
    assert await gate_redis.redis.get(story_told_key(CHAT, STORY)) is None


async def test_archived_story_is_silent_and_forgets_record(graph, client, gate_stories, gate_redis):
    gate_stories.put(STORY, status="waiting_human_review")
    await _turn(graph, client, _reminder())
    gate_stories.put(STORY, status="archived")
    await _turn(graph, client, _reminder())
    assert len(_told(client)) == 1
    assert await gate_redis.redis.get(story_told_key(CHAT, STORY)) is None


async def test_record_is_per_chat_and_has_no_periodic_expiry(
    graph, client, gate_stories, gate_redis
):
    gate_stories.put(STORY, status="waiting_human_review")
    await _turn(graph, client, _reminder())
    await _handle_message(graph, client, "other-chat", _reminder())
    assert len(_told(client)) == 2
    assert await gate_redis.redis.ttl(story_told_key(CHAT, STORY)) == -1


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

    assert _told(client) == []
    assert len(notifying_graph.tool_results) == 5
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


def test_reminder_tool_does_not_invite_progress_reminders():
    from src.agents.po.tools import set_reminder

    text = " ".join(set_reminder.description.split())
    assert "Do not set progress reminders after creating a story." in text
    assert "In-work stories get no reply" in text
