"""What a proactive PO message about a story may say: a change, or a new escalation step.

A PO turn that no user asked for — a fired ``set_reminder``, a scheduler stage
notice — ends in a message to the user whenever the model writes a reply. A
stuck story made that a loop: the PO re-set its reminder every quarter hour and
told the user "work is going" each time (tg 1015926438 got at least 14 of those
in a row, then one an hour from the stage notices). Asking the model to stay
quiet in its prompt is not a guarantee, so the guarantee lives here, at the one
place such a turn becomes a user message (``consumers/po.py``).

**The rule.** For a gated turn — a ``reminder`` or a ``story_stage`` event that
names a story — the reply is published only if

* the story's **fingerprint** (``status``, ``waiting_on``, the ``StoryFailure``
  code and the planning state with its failed-attempt count, read from the API
  now) differs from the one last told to this chat about this story, or
* the turn is a stage notice that is a **new escalation step**: a later stay
  (``stage_entered_at``) than the one last told, or a higher step
  (``stage_notice_step``) of the same stay — see
  ``scheduler/src/tasks/supervisor/stage_notices.py``;

and fewer than ``po.story_proactive_daily_cap`` gated messages about the story
reached the chat this UTC day. Anything else is suppressed and logged as
``po_proactive_suppressed`` with the reason. The PO turn itself has run and its
thread keeps it either way; only the message to the user is withheld.

**There is no second way out.** In any turn without a ``request_id`` the
``notify_user`` tool publishes nothing (the consumer passes ``user_turn`` in
the run config), so this final reply is the only thing such a turn can send.

**Other story turns are not gated.** The durable owner notifications (the
story's ending, a parked story, a secret request, returned requirements) are
told as before and never counted against the cap. After one is published, its
fingerprint is recorded as told, so the next reminder does not repeat it.

**Steps only rise.** ``po:input`` is at-least-once, so a stage notice may
arrive again after a later one was told. The record keeps the stay last told
(its stage and ``stage_entered_at``) with the highest step told in it, so a
lower or equal step of that stay, or any step of an older stay, is suppressed
whatever order redelivery brings it in. A return to the stage is a new stay
with a later ``stage_entered_at``, and its steps start over from 0.

**The "last told" record.** Per chat and story, Redis holds the fingerprint and
that stay (``po:story_told:<chat>:<story>``). It is written only after
the proactive entry was published: a publish that fails leaves the old record,
so the change is told again on the next turn — a duplicate is the smaller error
than a change nobody hears about. Turns for one chat run one at a time under
the consumer's per-chat lock, so the read and the write cannot interleave with
another turn of the same chat.

**It ends with the story.** An ending's owner notification
(``story_completed``/``story_failed``) deletes the record, and so does a gated
turn that reads the story in a terminal status — which also suppresses that
turn, since the ending is the durable seam's to tell. A record refreshed by no
turn for ``STORY_TOLD_TTL`` expires on its own, which covers a story archived
with no PO turn after it; the cost of that expiry is at most one repeated
message a month. The per-day counter expires after two days.

**When the gate cannot decide.** If the story or the cap cannot be read, the
reply is published and nothing is recorded (``po_proactive_gate_unavailable``):
the same "a duplicate is better than a lost change".
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import json
from typing import Protocol

import structlog

from shared.contracts.dto.story import STAGE_NOTICE_TERMINAL_STATUSES, StoryDTO
from shared.contracts.dto.story_failure import STORY_FAILURE_REASON
from shared.contracts.vocab import OwnerNotificationEvent
from shared.redis import RedisStreamClient

logger = structlog.get_logger(__name__)

DAILY_CAP_CONFIG_KEY = "po.story_proactive_daily_cap"

STORY_TOLD_KEY_PREFIX = "po:story_told:"
STORY_TOLD_DAILY_KEY_PREFIX = "po:story_told_daily:"

#: How long a record lives without being told again. Far longer than the stage
#: notices' capped gap (a day), so a story still in work never loses it.
STORY_TOLD_TTL = timedelta(days=30)
DAILY_COUNT_TTL = timedelta(days=2)

#: The owner notifications that announce the story's ending.
STORY_END_EVENTS = frozenset(
    {OwnerNotificationEvent.STORY_COMPLETED, OwnerNotificationEvent.STORY_FAILED}
)


class StoryReader(Protocol):
    async def get_story(self, story_id: str) -> StoryDTO: ...


@dataclass(frozen=True)
class StoryFingerprint:
    """What about a story the user is told about when it changes."""

    status: str
    waiting_on: str
    failure_code: str | None
    planning_state: str | None
    planning_failed_attempts: int | None

    @classmethod
    def of(cls, story: StoryDTO) -> StoryFingerprint:
        reason = story.quarantine_reason
        failure_code = (
            reason.get("code")
            if isinstance(reason, dict) and reason.get("reason") == STORY_FAILURE_REASON
            else None
        )
        planning = story.planning
        return cls(
            status=story.status.value,
            waiting_on=story.waiting_on.value,
            failure_code=failure_code,
            planning_state=planning.state.value if planning else None,
            planning_failed_attempts=planning.failed_attempts if planning else None,
        )

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "status": self.status,
            "waiting_on": self.waiting_on,
            "failure_code": self.failure_code,
            "planning_state": self.planning_state,
            "planning_failed_attempts": self.planning_failed_attempts,
        }


@dataclass(frozen=True)
class StayStep:
    """Where a stage notice falls: which stay in which stage, and its step there."""

    stage: str
    #: When the stay's entry notice went out; the scheduler's stay identity.
    entered_at: datetime
    step: int

    @classmethod
    def of_notice(cls, data: dict) -> StayStep | None:
        if data.get("event") != OwnerNotificationEvent.STORY_STAGE:
            return None
        return cls(
            stage=data["stage"],
            entered_at=datetime.fromisoformat(data["stage_entered_at"]),
            step=int(data["stage_notice_step"]),
        )

    def is_after(self, told: StayStep | None) -> bool:
        """A later stay than *told*, or a higher step of the same stay."""
        if told is None:
            return True
        if (self.stage, self.entered_at) == (told.stage, told.entered_at):
            return self.step > told.step
        return self.entered_at > told.entered_at

    def as_dict(self) -> dict[str, str | int]:
        return {
            "stage": self.stage,
            "stage_entered_at": self.entered_at.isoformat(),
            "max_step_told": self.step,
        }

    @classmethod
    def from_dict(cls, data: dict) -> StayStep:
        return cls(
            stage=data["stage"],
            entered_at=datetime.fromisoformat(data["stage_entered_at"]),
            step=int(data["max_step_told"]),
        )


@dataclass(frozen=True)
class ToldRecord:
    """The fingerprint and the stage stay last told to one chat about one story."""

    fingerprint: StoryFingerprint
    #: The latest stay told, with the highest step told in it; ``None`` before any.
    stay: StayStep | None

    def dumps(self) -> str:
        return json.dumps(
            {
                "fingerprint": self.fingerprint.as_dict(),
                "stay": self.stay.as_dict() if self.stay else None,
            }
        )

    @classmethod
    def loads(cls, raw: str) -> ToldRecord:
        data = json.loads(raw)
        stay = data["stay"]
        return cls(
            fingerprint=StoryFingerprint(**data["fingerprint"]),
            stay=StayStep.from_dict(stay) if stay is not None else None,
        )


@dataclass(frozen=True)
class ProactiveDecision:
    """Whether a turn's reply goes to the user, and what to record once it has."""

    send: bool
    reason: str
    story_id: str = ""
    gated: bool = False
    #: The story ended: the record is deleted instead of written.
    ends_story: bool = False
    fingerprint: StoryFingerprint | None = None
    stay: StayStep | None = None
    previous: ToldRecord | None = None


def story_told_key(telegram_chat_id: str, story_id: str) -> str:
    return f"{STORY_TOLD_KEY_PREFIX}{telegram_chat_id}:{story_id}"


def story_told_daily_key(telegram_chat_id: str, story_id: str, day: datetime) -> str:
    return f"{STORY_TOLD_DAILY_KEY_PREFIX}{telegram_chat_id}:{story_id}:{day.date().isoformat()}"


def _is_gated(data: dict) -> bool:
    msg_type = data.get("type")
    return msg_type == "reminder" or (
        msg_type == "system_event" and data.get("event") == OwnerNotificationEvent.STORY_STAGE
    )


class ProactiveStoryGate:
    """Decides a proactive reply about a story, and records what was told."""

    def __init__(
        self,
        redis_client: RedisStreamClient,
        stories: StoryReader,
        daily_cap: Callable[[], int],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._redis = redis_client
        self._stories = stories
        self._daily_cap = daily_cap
        self._clock = clock

    async def decide(self, telegram_chat_id: str, data: dict) -> ProactiveDecision:
        story_id = data.get("story_id") or ""
        if not story_id:
            return ProactiveDecision(send=True, reason="no_story")
        gated = _is_gated(data)
        log = logger.bind(telegram_chat_id=telegram_chat_id, story_id=story_id)
        if data.get("event") in STORY_END_EVENTS:
            return ProactiveDecision(
                send=True, reason="story_end_notice", story_id=story_id, ends_story=True
            )
        try:
            story = await self._stories.get_story(story_id)
            fingerprint = StoryFingerprint.of(story)
            previous = await self._read_record(telegram_chat_id, story_id)
            delivered_today = await self._delivered_today(telegram_chat_id, story_id)
            daily_cap = await asyncio.to_thread(self._daily_cap) if gated else None
        except Exception as exc:
            log.warning("po_proactive_gate_unavailable", gated=gated, error=str(exc))
            return ProactiveDecision(send=True, reason="gate_unavailable", story_id=story_id)

        if story.status in STAGE_NOTICE_TERMINAL_STATUSES:
            if gated:
                # The ending is the owner-notification seam's to tell, durably.
                await self._forget(telegram_chat_id, story_id)
                return self._suppressed(log, story_id, "story_ended", fingerprint)
            return ProactiveDecision(
                send=True, reason="story_end_notice", story_id=story_id, ends_story=True
            )

        decided = ProactiveDecision(
            send=True,
            reason="ungated",
            story_id=story_id,
            gated=gated,
            fingerprint=fingerprint,
            stay=StayStep.of_notice(data),
            previous=previous,
        )
        if not gated:
            return decided
        if previous is None or previous.fingerprint != fingerprint:
            reason = "changed"
        elif decided.stay is not None and decided.stay.is_after(previous.stay):
            reason = "escalation_step"
        else:
            return self._suppressed(log, story_id, "unchanged", fingerprint, stay=decided.stay)
        if delivered_today >= daily_cap:
            return self._suppressed(log, story_id, "daily_cap", fingerprint, stay=decided.stay)
        return replace(decided, reason=reason)

    async def record_told(self, telegram_chat_id: str, decision: ProactiveDecision) -> None:
        """After the publish: remember what the user was told, or forget an ended story."""
        if not decision.story_id:
            return
        if decision.ends_story:
            await self._forget(telegram_chat_id, decision.story_id)
            return
        if decision.fingerprint is None:
            return
        told_stay = decision.previous.stay if decision.previous else None
        # A change told by a stale notice keeps the later stay already told.
        if decision.stay is not None and decision.stay.is_after(told_stay):
            told_stay = decision.stay
        record = ToldRecord(fingerprint=decision.fingerprint, stay=told_stay)
        async with self._redis.redis.pipeline(transaction=True) as pipe:
            pipe.set(
                story_told_key(telegram_chat_id, decision.story_id),
                record.dumps(),
                ex=int(STORY_TOLD_TTL.total_seconds()),
            )
            if decision.gated:
                daily = story_told_daily_key(telegram_chat_id, decision.story_id, self._clock())
                pipe.incr(daily)
                pipe.expire(daily, int(DAILY_COUNT_TTL.total_seconds()))
            await pipe.execute()

    async def _read_record(self, telegram_chat_id: str, story_id: str) -> ToldRecord | None:
        raw = await self._redis.redis.get(story_told_key(telegram_chat_id, story_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            return ToldRecord.loads(raw)
        except (ValueError, KeyError, TypeError):
            # Unreadable proves nothing was told; telling once more rewrites it.
            logger.warning("po_story_told_record_unreadable", story_id=story_id)
            return None

    async def _delivered_today(self, telegram_chat_id: str, story_id: str) -> int:
        raw = await self._redis.redis.get(
            story_told_daily_key(telegram_chat_id, story_id, self._clock())
        )
        return int(raw) if raw is not None else 0

    async def _forget(self, telegram_chat_id: str, story_id: str) -> None:
        deleted = await self._redis.redis.delete(story_told_key(telegram_chat_id, story_id))
        if deleted:
            logger.info(
                "po_story_told_record_forgotten",
                telegram_chat_id=telegram_chat_id,
                story_id=story_id,
            )

    @staticmethod
    def _suppressed(
        log,
        story_id: str,
        reason: str,
        fingerprint: StoryFingerprint,
        *,
        stay: StayStep | None = None,
    ) -> ProactiveDecision:
        log.info(
            "po_proactive_suppressed",
            reason=reason,
            fingerprint=fingerprint.as_dict(),
            escalation_step=stay.as_dict() if stay else None,
        )
        return ProactiveDecision(send=False, reason=reason, story_id=story_id)
