"""Offline regressions for the stage-notice predicate and the record it judges.

`stage_notice_mismatches` is fed the observation a green level-1 story leaves —
entries into each stage it passed through, in order, before its ending — and
then the observation each defect would leave: silence, one notice per tick, a
repeat inside the quiet interval, a stage paired with the wrong wait, a notice
after the ending. A predicate that answered `[]` to any of those would pass a
stand run while the owner is flooded or left in silence.
"""

from __future__ import annotations

import httpx
from level1_stage_notices import stage_notice_mismatches
import pipeline_helpers
import pytest
import run_evidence

from shared.contracts.dto.story import (
    StoryStageNoticeKind,
    StoryStatus,
    StoryWaitEstimate,
    StoryWaitingOn,
)
from shared.contracts.queues.po import POSystemEvent

pytestmark = pytest.mark.needs_no_api_credential

STORY_ID = "story-l1"
QUIET = 60

_WAITS = {
    StoryStatus.CREATED: (StoryWaitingOn.NONE, StoryWaitEstimate.UNBOUNDED),
    StoryStatus.IN_PROGRESS: (StoryWaitingOn.NONE, StoryWaitEstimate.UNBOUNDED),
    StoryStatus.PR_REVIEW: (StoryWaitingOn.CI, StoryWaitEstimate.HOURS),
    StoryStatus.DEPLOYING: (StoryWaitingOn.DEPLOY, StoryWaitEstimate.TENS_OF_MINUTES),
    StoryStatus.TESTING: (StoryWaitingOn.QA, StoryWaitEstimate.TENS_OF_MINUTES),
}


def _notice(
    stage: StoryStatus,
    at: str,
    kind: StoryStageNoticeKind = StoryStageNoticeKind.ENTERED,
    *,
    story_id: str = STORY_ID,
) -> dict:
    """A notice exactly as `record_stage_notices` stores it: the event's JSON dump."""
    waiting_on, estimate = _WAITS[stage]
    return POSystemEvent(
        event="story_stage",
        text=f"Story is at stage {stage.value}.",
        telegram_chat_id="4242",
        story_id=story_id,
        project_id="project-1",
        timestamp=f"2026-09-21T{at}:00+00:00",
        stage=stage,
        waiting_on=waiting_on,
        wait_estimate=estimate,
        stage_notice=kind,
    ).model_dump(mode="json")


def _observation(*notices: dict, ended_at: str | None = "2026-09-21T12:30:00") -> dict:
    return {
        "story_id": STORY_ID,
        "quiet_minutes": QUIET,
        "ended_at": ended_at,
        "notices": list(notices),
    }


def _green() -> list[dict]:
    """A level-1 story: planned, built, checked, deployed, tested, then completed."""
    return [
        _notice(StoryStatus.CREATED, "12:00"),
        _notice(StoryStatus.IN_PROGRESS, "12:01"),
        _notice(StoryStatus.PR_REVIEW, "12:10"),
        _notice(StoryStatus.DEPLOYING, "12:18"),
        _notice(StoryStatus.TESTING, "12:25"),
    ]


def test_a_story_announced_once_per_stage_holds():
    assert stage_notice_mismatches(_observation(*_green())) == []


def test_a_repeat_after_the_quiet_interval_holds():
    notices = [
        _notice(StoryStatus.IN_PROGRESS, "10:00"),
        _notice(StoryStatus.IN_PROGRESS, "11:00", StoryStageNoticeKind.STILL_THERE),
        _notice(StoryStatus.PR_REVIEW, "11:05"),
    ]
    assert stage_notice_mismatches(_observation(*notices)) == []


def test_silence_is_named():
    """The 23:44 case: a story in work and nothing on the owner's side at all."""
    assert stage_notice_mismatches(_observation()) == [
        f"no story_stage notice about story {STORY_ID} reached po:input"
    ]


def test_one_notice_per_tick_is_named():
    notices = [
        _notice(StoryStatus.IN_PROGRESS, "12:01"),
        _notice(StoryStatus.IN_PROGRESS, "12:02"),
    ]
    assert stage_notice_mismatches(_observation(*notices)) == [
        "notice 1 announces entering in_progress again with no stage in between"
    ]


def test_a_repeat_inside_the_interval_is_named():
    """The flood: five messages in one hour."""
    notices = [
        _notice(StoryStatus.IN_PROGRESS, "12:00"),
        _notice(StoryStatus.IN_PROGRESS, "12:12", StoryStageNoticeKind.STILL_THERE),
    ]
    assert stage_notice_mismatches(_observation(*notices)) == [
        "notice 1 repeats in_progress 12.0 minutes after the last one, "
        "inside the 60-minute interval"
    ]


def test_a_repeat_of_a_stage_never_entered_is_named():
    notices = [
        _notice(StoryStatus.IN_PROGRESS, "10:00"),
        _notice(StoryStatus.PR_REVIEW, "11:00", StoryStageNoticeKind.STILL_THERE),
    ]
    assert stage_notice_mismatches(_observation(*notices)) == [
        "notice 1 repeats pr_review, which the notice before it did not announce"
    ]


def test_a_stage_paired_with_the_wrong_wait_is_named():
    notice = {**_notice(StoryStatus.DEPLOYING, "12:18"), "waiting_on": "qa"}
    assert stage_notice_mismatches(_observation(notice)) == [
        "notice 0 says deploying waits on 'qa', but deploying waits on 'deploy'"
    ]


def test_a_notice_for_a_parked_or_ended_stage_is_named():
    notice = {**_notice(StoryStatus.DEPLOYING, "12:18"), "stage": "waiting_user_secret"}
    assert stage_notice_mismatches(_observation(notice)) == [
        "notice 0 names stage 'waiting_user_secret', which is not a stage in work"
    ]


def test_a_notice_without_an_estimate_or_a_chat_is_named():
    notice = {
        **_notice(StoryStatus.TESTING, "12:25"),
        "wait_estimate": None,
        "telegram_chat_id": "",
    }
    assert stage_notice_mismatches(_observation(notice)) == [
        "notice 0 is addressed to no Telegram chat",
        "notice 0 carries no wait estimate (None)",
    ]


def test_a_notice_after_the_ending_is_named():
    notices = [*_green(), _notice(StoryStatus.TESTING, "12:40", StoryStageNoticeKind.ENTERED)]
    reasons = stage_notice_mismatches(_observation(*notices))
    assert reasons == [
        "notice 5 announces entering testing again with no stage in between",
        "notice 5 (testing) was sent at 2026-09-21T12:40:00+00:00, "
        "after the story ended at 2026-09-21T12:30:00+00:00",
    ]


def test_another_storys_notice_and_a_first_repeat_are_named():
    notices = [
        _notice(StoryStatus.IN_PROGRESS, "10:00", StoryStageNoticeKind.STILL_THERE),
        _notice(StoryStatus.PR_REVIEW, "10:10", story_id="story-other"),
    ]
    reasons = stage_notice_mismatches(_observation(*notices))
    assert "notice 1 is about story 'story-other', not 'story-l1'" in reasons
    assert "the first notice is 'still_there', not the entry into a stage" in reasons


def test_an_observation_without_its_interval_is_refused():
    observation = {**_observation(*_green()), "quiet_minutes": None}
    assert stage_notice_mismatches(observation) == [
        "the observation carries no quiet interval (None)"
    ]


# ── the record the stand leaves ──────────────────────────────────────────


def _event(**fields) -> POSystemEvent:
    return POSystemEvent.model_validate(fields)


@pytest.mark.asyncio
async def test_the_record_keeps_only_this_storys_stage_notices_and_the_stands_interval():
    ctx = {
        "story_id": STORY_ID,
        "po_input_cursor": "10-0",
        "story_terminal": {"status": "completed", "updated_at": "2026-09-21T12:30:00"},
    }
    ours = _notice(StoryStatus.IN_PROGRESS, "12:01")
    events = [
        _event(**ours),
        _event(**_notice(StoryStatus.PR_REVIEW, "12:02", story_id="story-other")),
        _event(event="story_completed", text="done", story_id=STORY_ID, telegram_chat_id="4242"),
    ]
    cursors: list[str] = []

    def events_after(cursor: str) -> list[POSystemEvent]:
        cursors.append(cursor)
        return events

    def config(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/system-configs/supervisor.stage_notice_quiet_minutes"
        return httpx.Response(200, json={"value": 45})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(config)
    ) as api:
        await pipeline_helpers.record_stage_notices(api, ctx, events_after=events_after)

    assert cursors == ["10-0"]
    assert ctx["stage_notices"] == {
        "story_id": STORY_ID,
        "quiet_minutes": 45,
        "ended_at": "2026-09-21T12:30:00",
        "notices": [ours],
    }
    assert "stage_notices_error" not in ctx


@pytest.mark.asyncio
async def test_a_record_that_could_not_be_read_says_why():
    ctx = {"story_id": STORY_ID, "po_input_cursor": "10-0"}

    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
    ) as api:
        await pipeline_helpers.record_stage_notices(api, ctx, events_after=lambda _c: [])

    assert "stage_notices" not in ctx
    assert ctx["stage_notices_error"].startswith(
        f"stage notices of story {STORY_ID} could not be read: HTTPStatusError"
    )


def test_the_artifact_carries_both_stories_notices():
    first = _observation(*_green())
    second = {**_observation(_notice(StoryStatus.IN_PROGRESS, "13:00")), "story_id": "story-l2"}
    ctx = {"stage_notices": first, "level1_extension": {"stage_notices": second}}

    evidence = run_evidence.stage_notice_evidence(ctx)

    assert evidence["first_story"] == {"status": "captured", "value": first, "reason": None}
    assert evidence["second_story"] == {"status": "captured", "value": second, "reason": None}


def test_the_artifact_states_a_second_story_whose_read_failed():
    ctx = {
        "stage_notices": _observation(*_green()),
        "level1_extension": {"story_id": "story-l2", "stage_notices_error": "redis gone"},
    }

    evidence = run_evidence.stage_notice_evidence(ctx)

    assert evidence["second_story"] == {"status": "missed", "value": None, "reason": "redis gone"}
