"""Whether a story's owner was told its stages, stated as a predicate over the record.

issue:b28d93: one owner's PO went silent for the whole of a story while another
got five messages in an hour. The scheduler now announces each in-work stage on
entry and again after a quiet interval (`supervisor/stage_notices.py`), and the
thing a run can show about that is the `story_stage` events it left on
`po:input` — typed fields, not composed text, since the level-1 run calls no
model and PO's words are not what is being judged.

The property is the one the scheduler promises: the owner is told each stage the
story is *observed* in, within one sweep of observing it, and again after the
quiet interval while it stays there. A stage entered and left between two
sweeps is deliberately not announced. So the notices are compared with the
stages the harness itself observed — the API keeps no transition history, so
the run samples the story's status on its own clock — and every stage the
harness saw for longer than one dispatcher interval plus
`SWEEP_MARGIN_SECONDS` must have been announced. A shorter stay may or may not
be; either is correct.

`stage_notice_mismatches` is a function from one story's observation to the
reasons it does not hold, the shape `level1_second_story` uses: `[]` means the
property holds, and the offline regressions feed it the observation each defect
would leave to prove it says so.

The observation is recorded by `pipeline_helpers.record_stage_notices`:

* ``story_id`` — the story the notices must be about;
* ``quiet_minutes`` — the stand's own `supervisor.stage_notice_quiet_minutes`;
* ``dispatch_interval_seconds`` — the stand's `scheduler.dispatch_interval_seconds`;
* ``ended_at`` — when the story reached its ending, as the API reported it;
* ``observed_stages`` — the harness's own samples, ``{sample_interval_seconds,
  read_errors, spans}``, each span ``{stage, first_seen, last_seen, samples}``
  as `extend_stage_spans` builds it;
* ``notices`` — every `story_stage` event for the story after the run's cursor,
  in stream order, as `POSystemEvent.model_dump(mode="json")`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.contracts.dto.story import (
    STAGE_NOTICE_STATUSES,
    WAITING_ON_BY_STATUS,
    StoryStageNoticeKind,
    StoryStatus,
    StoryWaitEstimate,
)
from shared.contracts.vocab import OwnerNotificationEvent

_STAGES = {status.value for status in STAGE_NOTICE_STATUSES}

#: What a stay must exceed, beyond one dispatcher interval, before a notice is
#: owed for it. The sweep runs after every other supervisor of the tick, whose
#: GitHub and API calls take seconds, and the tick's sleep starts only after the
#: work; 30 s — one more default interval — covers a slow tick, and the
#: harness's own sampling (5 s) under-measures a stay rather than inflating it.
SWEEP_MARGIN_SECONDS = 30


def extend_stage_spans(spans: list[dict], stage: str, at: datetime) -> None:
    """Add one sample of the story's status to the harness's list of stays.

    A sample of the stage the last span is in extends it; any other opens a new
    span. So a span's ``last_seen - first_seen`` is a lower bound on the stay.
    """
    moment = at.isoformat()
    if spans and spans[-1]["stage"] == stage:
        spans[-1]["last_seen"] = moment
        spans[-1]["samples"] += 1
        return
    spans.append({"stage": stage, "first_seen": moment, "last_seen": moment, "samples": 1})


_ESTIMATES = {estimate.value for estimate in StoryWaitEstimate}


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # The API writes naive UTC; the scheduler writes aware UTC. One clock.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _notice_mismatches(index: int, notice: dict, story_id: str) -> list[str]:
    """What is wrong with one notice on its own."""
    label = f"notice {index}"
    reasons = []
    if notice.get("event") != OwnerNotificationEvent.STORY_STAGE.value:
        reasons.append(f"{label} is event {notice.get('event')!r}, not story_stage")
    if notice.get("story_id") != story_id:
        reasons.append(f"{label} is about story {notice.get('story_id')!r}, not {story_id!r}")
    if not notice.get("telegram_chat_id"):
        reasons.append(f"{label} is addressed to no Telegram chat")
    stage = notice.get("stage")
    if stage not in _STAGES:
        reasons.append(f"{label} names stage {stage!r}, which is not a stage in work")
    else:
        expected = WAITING_ON_BY_STATUS[StoryStatus(stage)].value
        if notice.get("waiting_on") != expected:
            reasons.append(
                f"{label} says {stage} waits on {notice.get('waiting_on')!r}, "
                f"but {stage} waits on {expected!r}"
            )
    if notice.get("wait_estimate") not in _ESTIMATES:
        reasons.append(f"{label} carries no wait estimate ({notice.get('wait_estimate')!r})")
    if notice.get("stage_notice") not in {kind.value for kind in StoryStageNoticeKind}:
        reasons.append(f"{label} does not say why it was sent ({notice.get('stage_notice')!r})")
    return reasons


def _sequence_mismatches(notices: list[dict], quiet_minutes: float) -> list[str]:
    """What is wrong with the notices as a sequence: entries, repeats and spacing.

    Two entries into the same stage in a row are the one-per-tick defect. The
    only legitimate way to produce them is a detour through an owner-told state,
    which is silent by design; the level-1 route has none (its bot token is
    supplied with the brief, so no story waits on a secret or a reviewer), so on
    this route that shape is a defect.
    """
    reasons = []
    previous: dict | None = None
    for index, notice in enumerate(notices):
        stage, kind = notice.get("stage"), notice.get("stage_notice")
        same_stage = previous is not None and previous.get("stage") == stage
        if kind == StoryStageNoticeKind.ENTERED.value and same_stage:
            reasons.append(
                f"notice {index} announces entering {stage} again with no stage in between"
            )
        if kind == StoryStageNoticeKind.STILL_THERE.value:
            if not same_stage:
                reasons.append(
                    f"notice {index} repeats {stage}, which the notice before it did not announce"
                )
            else:
                since, at = _moment(previous.get("timestamp")), _moment(notice.get("timestamp"))
                if since is None or at is None:
                    reasons.append(f"notice {index} or the one before it carries no timestamp")
                elif (at - since).total_seconds() / 60 < quiet_minutes:
                    reasons.append(
                        f"notice {index} repeats {stage} {(at - since).total_seconds() / 60:.1f} "
                        f"minutes after the last one, inside the {quiet_minutes}-minute interval"
                    )
        previous = notice
    return reasons


def _unannounced_stage_mismatches(
    notices: list[dict], observed: object, dispatch_interval: object
) -> list[str]:
    """Every stage the harness saw for longer than one sweep, with no entry notice for it.

    Counted per stage, so a stage visited twice for long needs two entries:
    the order of the two sides cannot be compared on one clock, their counts can.
    """
    if not isinstance(dispatch_interval, int | float) or dispatch_interval <= 0:
        return [f"the observation carries no dispatcher interval ({dispatch_interval!r})"]
    spans = observed.get("spans") if isinstance(observed, dict) else None
    if not isinstance(spans, list) or not spans:
        return ["the harness recorded no stages of the story to compare the notices with"]
    threshold = dispatch_interval + SWEEP_MARGIN_SECONDS
    entered: dict[str, int] = {}
    for notice in notices:
        if notice.get("stage_notice") == StoryStageNoticeKind.ENTERED.value:
            entered[notice.get("stage")] = entered.get(notice.get("stage"), 0) + 1
    owed: dict[str, list[dict]] = {}
    for span in spans:
        first, last = _moment(span.get("first_seen")), _moment(span.get("last_seen"))
        if span.get("stage") not in _STAGES or first is None or last is None:
            continue
        if (last - first).total_seconds() > threshold:
            owed.setdefault(span["stage"], []).append(span)
    reasons = []
    for stage, stays in owed.items():
        announced = entered.get(stage, 0)
        for stay in stays[announced:]:
            seen = (_moment(stay["last_seen"]) - _moment(stay["first_seen"])).total_seconds()
            reasons.append(
                f"the harness saw the story in {stage} for {seen:.0f}s "
                f"({stay['first_seen']} to {stay['last_seen']}), longer than one dispatcher "
                f"interval ({dispatch_interval}s) plus {SWEEP_MARGIN_SECONDS}s, and no "
                f"notice announced entering it ({announced} entry notices for "
                f"{len(stays)} such stays)"
            )
    return reasons


def stage_notice_mismatches(observation: dict) -> list[str]:
    """Why this story's owner was not told its stages the way the contract says."""
    story_id = observation.get("story_id")
    notices = observation.get("notices")
    quiet_minutes = observation.get("quiet_minutes")
    if not story_id:
        return ["the observation names no story"]
    if not isinstance(quiet_minutes, int | float) or quiet_minutes <= 0:
        return [f"the observation carries no quiet interval ({quiet_minutes!r})"]
    if not isinstance(notices, list) or not notices:
        return [f"no story_stage notice about story {story_id} reached po:input"]

    reasons: list[str] = []
    for index, notice in enumerate(notices):
        reasons.extend(_notice_mismatches(index, notice, story_id))
    if notices[0].get("stage_notice") != StoryStageNoticeKind.ENTERED.value:
        reasons.append(
            f"the first notice is {notices[0].get('stage_notice')!r}, not the entry into a stage"
        )
    reasons.extend(_sequence_mismatches(notices, quiet_minutes))
    reasons.extend(
        _unannounced_stage_mismatches(
            notices,
            observation.get("observed_stages"),
            observation.get("dispatch_interval_seconds"),
        )
    )

    ended_at = _moment(observation.get("ended_at"))
    if ended_at is not None:
        for index, notice in enumerate(notices):
            at = _moment(notice.get("timestamp"))
            if at is not None and at > ended_at:
                reasons.append(
                    f"notice {index} ({notice.get('stage')}) was sent at {at.isoformat()}, "
                    f"after the story ended at {ended_at.isoformat()}"
                )
    return reasons
