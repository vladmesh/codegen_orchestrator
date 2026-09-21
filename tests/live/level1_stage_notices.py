"""Whether a story's owner was told its stages, stated as a predicate over the record.

issue:b28d93: one owner's PO went silent for the whole of a story while another
got five messages in an hour. The scheduler now announces each in-work stage on
entry and again after a quiet interval (`supervisor/stage_notices.py`), and the
thing a run can show about that is the `story_stage` events it left on
`po:input` — typed fields, not composed text, since the level-1 run calls no
model and PO's words are not what is being judged.

`stage_notice_mismatches` is a function from one story's observation to the
reasons it does not hold, the shape `level1_second_story` uses: `[]` means the
property holds, and the offline regressions feed it the observation each defect
would leave to prove it says so.

The observation is recorded by `pipeline_helpers.record_stage_notices`:

* ``story_id`` — the story the notices must be about;
* ``quiet_minutes`` — the stand's own `supervisor.stage_notice_quiet_minutes`;
* ``ended_at`` — when the story reached its ending, as the API reported it;
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
