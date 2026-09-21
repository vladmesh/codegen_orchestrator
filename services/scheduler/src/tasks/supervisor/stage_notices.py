"""Tell a story's owner which stage it is at while it is in work.

A story in work used to be silent between its creation and its ending, while a
story being repaired sent message after message. Both read the opposite of what
they mean: silence reads as "it disappeared", a flood as "it is broken". So each
story in a stage of `STAGE_NOTICE_STATUSES` produces one notice when it is first
seen in that stage and one more each time it is still there a quiet interval
after the last one. A notice names the stage (`StoryStatus`), what it waits for
(`WAITING_ON_BY_STATUS`) and the order of magnitude of the wait, read from the
state's configured bound in `state_age.STATE_AGE_BOUNDS` — the number that ends
the wait is the number the owner is told about. A stage no bound covers says so
(`UNBOUNDED`) instead of inventing one.

Nothing is sent for a story in `STAGE_NOTICE_TERMINAL_STATUSES` or
`STAGE_NOTICE_OWNER_TOLD_STATUSES`: the sweep never reads the first, and for
the second it only forgets what it last announced, so a story that comes back
into work is announced again on entry.

**Not a durable obligation.** The terminal owner-notification seam
(`tasks/owner_notifications.py`) exists because an ending that is lost is lost
for ever. A stage notice that is lost is superseded by the next one at most one
interval later, and one delivered late is already stale. So there is no owed
record and no recovery sweep, and `OwnerNotification` refuses the event outright.

**The marker is what makes it at-most-once per interval.** Per story, Redis
holds the stage last announced and when (`story:stage_notice:<id>`). It lives
outside the scheduler process, as the architect retry counter does, and Redis
runs with `appendonly`, so a scheduler restart reads the same marker back and
neither repeats a notice nor restarts the interval. The interval is measured
from the marker's `notified_at`, never from the tick or the process start. The
marker is written *before* the publish: a publish that fails after it costs one
notice, and the reverse order would let a failed marker write send the same
notice again on the next tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from typing import TYPE_CHECKING

import structlog

from shared.contracts.dto.story import (
    STAGE_NOTICE_OWNER_TOLD_STATUSES,
    STAGE_NOTICE_STATUSES,
    WAITING_ON_BY_STATUS,
    StoryDTO,
    StoryStageNoticeKind,
    StoryStatus,
    StoryWaitEstimate,
    StoryWaitingOn,
    story_wait_estimate,
)
from shared.contracts.queues.po import POSystemEvent, to_flat_fields
from shared.contracts.vocab import OwnerNotificationEvent
from shared.queues import PO_INPUT_QUEUE
from shared.redis import RedisStreamClient

if TYPE_CHECKING:
    from ...clients.api import SchedulerAPIClient

from ... import startup
from .._recipients import resolve_project_recipient
from .common import _parse_datetime
from .state_age import configured_bound_minutes

logger = structlog.get_logger(__name__)

STAGE_NOTICE_KEY_PREFIX = "story:stage_notice:"

#: The marker outlives its story's stay in work by one interval, so a story that
#: ends is forgotten on its own while one still in work always finds it: every
#: interval refreshes it. A scheduler outage longer than that turns the next
#: notice into an `entered` one — never into a second notice inside an interval.
_MARKER_TTL_INTERVALS = 2

#: What each stage is, in words the owner's PO can relay.
_STAGE_WORDS: dict[StoryStatus, str] = {
    StoryStatus.CREATED: "the change is being planned",
    StoryStatus.IN_PROGRESS: "the change is being built",
    StoryStatus.REOPENED: "the change was reopened and is about to be built again",
    StoryStatus.PR_REVIEW: "the finished code is being checked before it is merged",
    StoryStatus.DEPLOYING: "the change is being deployed",
    StoryStatus.TESTING: "the deployed change is being tested automatically",
}

#: What the system is waiting for, keyed by the typed `waiting_on`.
_WAITING_WORDS: dict[StoryWaitingOn, str] = {
    StoryWaitingOn.NONE: "its own work on this stage to finish",
    StoryWaitingOn.CI: "the automatic checks (CI) on the pull request",
    StoryWaitingOn.DEPLOY: "the deployment to finish",
    StoryWaitingOn.QA: "the automatic tests to report",
}

_ESTIMATE_WORDS: dict[StoryWaitEstimate, str] = {
    StoryWaitEstimate.MINUTES: "a few minutes",
    StoryWaitEstimate.TENS_OF_MINUTES: "tens of minutes",
    StoryWaitEstimate.HOURS: "a few hours",
    StoryWaitEstimate.DAYS: "a day or more",
}


def _quiet_interval_minutes() -> int:
    return startup.get_config().get_int("supervisor.stage_notice_quiet_minutes")


@dataclass(frozen=True)
class StageNoticeMarker:
    """The stage last announced for a story, and when."""

    stage: StoryStatus
    notified_at: datetime

    def dumps(self) -> str:
        return json.dumps({"stage": self.stage.value, "notified_at": self.notified_at.isoformat()})

    @classmethod
    def loads(cls, raw: str) -> StageNoticeMarker:
        data = json.loads(raw)
        return cls(
            stage=StoryStatus(data["stage"]), notified_at=_parse_datetime(data["notified_at"])
        )


def stage_notice_key(story_id: str) -> str:
    return f"{STAGE_NOTICE_KEY_PREFIX}{story_id}"


async def read_stage_notice_marker(
    redis_client: RedisStreamClient, story_id: str
) -> StageNoticeMarker | None:
    raw = await redis_client.redis.get(stage_notice_key(story_id))
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    return StageNoticeMarker.loads(raw)


def _notice_due(
    marker: StageNoticeMarker | None, stage: StoryStatus, now: datetime, interval: timedelta
) -> StoryStageNoticeKind | None:
    """Which notice this story is owed now, if any."""
    if marker is None or marker.stage is not stage:
        return StoryStageNoticeKind.ENTERED
    notified_at = marker.notified_at
    if notified_at.tzinfo is None:
        notified_at = notified_at.replace(tzinfo=UTC)
    if now - notified_at >= interval:
        return StoryStageNoticeKind.STILL_THERE
    return None


def stage_notice_text(
    story: StoryDTO,
    *,
    kind: StoryStageNoticeKind,
    bound_minutes: int | None,
    since: datetime | None,
    now: datetime,
) -> str:
    """The producer's statement of the notice; PO turns it into the owner's words."""
    stage = story.status
    waiting_on = WAITING_ON_BY_STATUS[stage]
    estimate = story_wait_estimate(bound_minutes)
    if kind is StoryStageNoticeKind.ENTERED:
        opening = f"Story '{story.title}' is now at stage {stage.value}: {_STAGE_WORDS[stage]}."
    else:
        minutes = round((now - since).total_seconds() / 60) if since else None
        opening = (
            f"Story '{story.title}' is still at stage {stage.value}: {_STAGE_WORDS[stage]}"
            + (f" ({minutes} minutes since the last update)." if minutes is not None else ".")
        )
    if estimate is StoryWaitEstimate.UNBOUNDED:
        wait = "This stage has no configured upper bound, so no time estimate is given."
    else:
        wait = (
            f"This stage takes up to {_ESTIMATE_WORDS[estimate]} "
            f"(its configured upper bound is {bound_minutes} minutes)."
        )
    return (
        f"{opening} The system is waiting for {_WAITING_WORDS[waiting_on]}. {wait} "
        "Nothing is needed from the owner; the next update comes on its own."
    )


async def supervise_stage_notices(
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Announce every in-work story's stage once per entry and once per quiet interval.

    Returns how many notices were sent on entry, how many repeated a stage, and
    how many were due but had no owner chat to go to.
    """
    now = now or datetime.now(UTC)
    interval = timedelta(minutes=_quiet_interval_minutes())
    ttl_seconds = int(interval.total_seconds()) * _MARKER_TTL_INTERVALS
    counts = {"entered": 0, "still_there": 0, "unaddressable": 0}

    # Forget the stage of a story parked on its owner, so coming back into work
    # is announced as an entry however soon it happens.
    for status in sorted(STAGE_NOTICE_OWNER_TOLD_STATUSES):
        for story in await api_client.get_stories_by_status(status):
            await redis_client.redis.delete(stage_notice_key(story.id))

    for status in sorted(STAGE_NOTICE_STATUSES):
        for story in await api_client.get_stories_by_status(status):
            log = logger.bind(story_id=story.id, project_id=str(story.project_id), stage=status)
            # One story's broken notice must not silence the others.
            try:
                kind = await _announce(
                    api_client, redis_client, story, now=now, interval=interval, ttl=ttl_seconds
                )
            except Exception:
                log.exception("stage_notice_failed")
                continue
            if kind is not None:
                counts[kind] += 1
    return counts


async def _announce(  # noqa: PLR0913 — one story's notice, each input named
    api_client: SchedulerAPIClient,
    redis_client: RedisStreamClient,
    story: StoryDTO,
    *,
    now: datetime,
    interval: timedelta,
    ttl: int,
) -> str | None:
    stage = story.status
    log = logger.bind(story_id=story.id, project_id=str(story.project_id), stage=stage.value)
    try:
        marker = await read_stage_notice_marker(redis_client, story.id)
    except (ValueError, KeyError):
        # An unreadable marker cannot prove a notice was sent in this interval,
        # and it cannot prove one was not; announcing the entry once is the
        # smaller error, and rewriting the marker ends it.
        log.warning("stage_notice_marker_unreadable")
        marker = None
    kind = _notice_due(marker, stage, now, interval)
    if kind is None:
        return None

    project_id = str(story.project_id)
    recipient = await resolve_project_recipient(
        api_client, project_id, event=OwnerNotificationEvent.STORY_STAGE.value, story_id=story.id
    )
    bound_minutes = configured_bound_minutes(stage)
    event = POSystemEvent(
        event=OwnerNotificationEvent.STORY_STAGE,
        text=stage_notice_text(
            story,
            kind=kind,
            bound_minutes=bound_minutes,
            since=marker.notified_at if marker else None,
            now=now,
        ),
        telegram_chat_id=recipient.telegram_chat_id,
        owner_user_id=recipient.owner_user_id,
        story_id=story.id,
        project_id=project_id,
        stage=stage,
        waiting_on=WAITING_ON_BY_STATUS[stage],
        wait_estimate=story_wait_estimate(bound_minutes),
        stage_notice=kind,
    )
    # Written first: see the module docstring for why at-most-once is the order.
    await redis_client.redis.set(
        stage_notice_key(story.id),
        StageNoticeMarker(stage=stage, notified_at=now).dumps(),
        ex=ttl,
    )
    if not recipient.is_addressable:
        # The resolver has already alerted administrators; PO would only refuse
        # an event addressed to nobody and alert them a second time.
        log.warning("stage_notice_unaddressable", reason=recipient.unaddressed_reason)
        return "unaddressable"
    await redis_client.publish_flat(PO_INPUT_QUEUE, to_flat_fields(event))
    log.info(
        "stage_notice_sent",
        stage_notice=kind.value,
        waiting_on=event.waiting_on.value,
        wait_estimate=event.wait_estimate.value,
    )
    return kind.value
