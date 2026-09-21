"""Offline regressions for the stage-notice predicate and the record it judges.

`stage_notice_mismatches` is fed the observation a green level-1 story leaves —
entries into each stage it passed through, in order, before its ending — and
then the observation each defect would leave: silence, a valid prefix that
stops announcing stages the harness went on to observe for minutes, one notice
per tick, a repeat inside the quiet interval, a stage paired with the wrong
wait, a notice after the ending. A predicate that answered `[]` to any of those would pass a
stand run while the owner is flooded or left in silence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from level1_stage_notices import (
    SWEEP_MARGIN_SECONDS,
    extend_stage_spans,
    stage_notice_mismatches,
)
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


DISPATCH = 30

#: What the harness saw of a green level-1 story: every stage for minutes.
_GREEN_STAGES = (
    (StoryStatus.CREATED, "12:00", "12:01"),
    (StoryStatus.IN_PROGRESS, "12:01", "12:10"),
    (StoryStatus.PR_REVIEW, "12:10", "12:18"),
    (StoryStatus.DEPLOYING, "12:18", "12:25"),
    (StoryStatus.TESTING, "12:25", "12:30"),
)


def _stays(*stays: tuple[StoryStatus, str, str]) -> dict:
    """The harness's sampled stays, in the shape `record_stage_notices` stores."""
    return {
        "sample_interval_seconds": 5.0,
        "read_errors": 0,
        "spans": [
            {
                "stage": stage.value,
                "first_seen": f"2026-09-21T{first}:00+00:00",
                "last_seen": f"2026-09-21T{last}:00+00:00",
                "samples": 12,
            }
            for stage, first, last in stays
        ],
    }


def _observation(
    *notices: dict,
    ended_at: str | None = "2026-09-21T12:30:00",
    observed: dict | None = None,
) -> dict:
    """One story's record. By default the harness saw only the stages notified."""
    if observed is None:
        # Momentary stays: nothing is owed by them, so these fixtures judge only
        # the notices. A harness side with nothing in it is its own finding.
        observed = _stays(
            (StoryStatus.CREATED, "12:00", "12:00"),
            *(
                (StoryStatus(n["stage"]), n["timestamp"][11:16], n["timestamp"][11:16])
                for n in notices
                if n.get("stage") in {status.value for status in _WAITS}
            ),
        )
    return {
        "story_id": STORY_ID,
        "quiet_minutes": QUIET,
        "dispatch_interval_seconds": DISPATCH,
        "ended_at": ended_at,
        "observed_stages": observed,
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
    assert stage_notice_mismatches(_observation(*_green(), observed=_stays(*_GREEN_STAGES))) == []


def test_a_valid_prefix_that_stops_announcing_is_named():
    """The review's example: `created` announced, then silence through four long stages."""
    observation = _observation(
        _notice(StoryStatus.CREATED, "12:00"), observed=_stays(*_GREEN_STAGES)
    )
    reasons = stage_notice_mismatches(observation)
    assert [reason.split(" for ")[0] for reason in reasons] == [
        "the harness saw the story in in_progress",
        "the harness saw the story in pr_review",
        "the harness saw the story in deploying",
        "the harness saw the story in testing",
    ]
    assert reasons[0] == (
        "the harness saw the story in in_progress for 540s "
        "(2026-09-21T12:01:00+00:00 to 2026-09-21T12:10:00+00:00), longer than one "
        f"dispatcher interval ({DISPATCH}s) plus {SWEEP_MARGIN_SECONDS}s, and no notice "
        "announced entering it (0 entry notices for 1 such stays)"
    )


def test_a_stage_shorter_than_one_sweep_is_not_owed():
    """Entered and left between two sweeps: announcing it would be false, so it is not required."""
    stays = _stays(
        (StoryStatus.CREATED, "12:00", "12:00"),
        (StoryStatus.IN_PROGRESS, "12:00", "12:10"),
    )
    stays["spans"][0]["last_seen"] = "2026-09-21T12:00:55+00:00"
    observation = _observation(_notice(StoryStatus.IN_PROGRESS, "12:01"), observed=stays)
    assert stage_notice_mismatches(observation) == []


def test_a_long_stage_visited_twice_needs_two_entries():
    stays = _stays(
        (StoryStatus.IN_PROGRESS, "12:00", "12:10"),
        (StoryStatus.PR_REVIEW, "12:10", "12:15"),
        (StoryStatus.IN_PROGRESS, "12:15", "12:25"),
    )
    notices = [
        _notice(StoryStatus.IN_PROGRESS, "12:00"),
        _notice(StoryStatus.PR_REVIEW, "12:10"),
    ]
    [reason] = stage_notice_mismatches(_observation(*notices, observed=stays))
    assert reason.startswith("the harness saw the story in in_progress for 600s")
    assert reason.endswith("(1 entry notices for 2 such stays)")


def test_an_observation_without_the_harness_side_is_refused():
    observation = {**_observation(*_green()), "observed_stages": None}
    assert stage_notice_mismatches(observation) == [
        "the harness recorded no stages of the story to compare the notices with"
    ]
    observation = {**_observation(*_green()), "dispatch_interval_seconds": None}
    assert stage_notice_mismatches(observation) == [
        "the observation carries no dispatcher interval (None)"
    ]


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
        values = {
            "/api/system-configs/supervisor.stage_notice_quiet_minutes": 45,
            "/api/system-configs/scheduler.dispatch_interval_seconds": 30,
        }
        return httpx.Response(200, json={"value": values[request.url.path]})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(config)
    ) as api:
        await pipeline_helpers.record_stage_notices(api, ctx, events_after=events_after)

    assert cursors == ["10-0"]
    assert ctx["stage_notices"] == {
        "story_id": STORY_ID,
        "quiet_minutes": 45,
        "dispatch_interval_seconds": 30,
        "ended_at": "2026-09-21T12:30:00",
        "observed_stages": None,
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


def test_samples_of_one_stage_extend_its_stay():
    spans: list[dict] = []
    for second, stage in ((0, "created"), (5, "created"), (10, "in_progress"), (15, "in_progress")):
        extend_stage_spans(spans, stage, datetime(2026, 9, 21, 12, 0, second, tzinfo=UTC))
    assert spans == [
        {
            "stage": "created",
            "first_seen": "2026-09-21T12:00:00+00:00",
            "last_seen": "2026-09-21T12:00:05+00:00",
            "samples": 2,
        },
        {
            "stage": "in_progress",
            "first_seen": "2026-09-21T12:00:10+00:00",
            "last_seen": "2026-09-21T12:00:15+00:00",
            "samples": 2,
        },
    ]


@pytest.mark.asyncio
async def test_the_harness_samples_the_story_until_the_record_stops_it():
    """The observed stages are the harness's own reads, stopped and stored with the notices."""
    statuses = iter(["created", "in_progress", "in_progress", "pr_review"])
    last = {"status": "created"}

    def api(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/stories/{STORY_ID}":
            last["status"] = next(statuses, last["status"])
            return httpx.Response(200, json={"id": STORY_ID, "status": last["status"]})
        return httpx.Response(200, json={"value": 30})

    ctx = {"story_id": STORY_ID, "po_input_cursor": "0-0"}
    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(api)) as c:
        pipeline_helpers.start_story_stage_observation(c, ctx, interval=0.001)
        for _ in range(50):
            await asyncio.sleep(0.002)
        await pipeline_helpers.record_stage_notices(c, ctx, events_after=lambda _c: [])
        await pipeline_helpers.stop_story_stage_observations(ctx)

    observed = ctx["stage_notices"]["observed_stages"]
    assert [span["stage"] for span in observed["spans"]] == ["created", "in_progress", "pr_review"]
    assert observed["read_errors"] == 0
    assert "story_stage_sampler" not in ctx
    assert "story_stage_sampler_tasks" not in ctx


@pytest.mark.asyncio
async def test_a_sampler_a_raised_phase_left_running_is_stopped():
    def api(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "in_progress"})

    ctx = {"story_id": STORY_ID}
    async with httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(api)) as c:
        pipeline_helpers.start_story_stage_observation(c, ctx, interval=0.001)
        _sampler, task, _interval = ctx["story_stage_sampler"]
        await asyncio.sleep(0.01)
        await pipeline_helpers.stop_story_stage_observations(ctx)

    assert task.cancelled()
