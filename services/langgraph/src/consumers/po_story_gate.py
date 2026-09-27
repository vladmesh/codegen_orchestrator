"""Only untold key changes can turn a story reminder into a user message.

The PO graph may write progress prose; this gate at the single proactive publish
point withholds it. Reminders can tell only needs_user or stopped, different from
what this chat last heard; a reminder about no story speaks only if the user
asked for it in their own turn. A planning being retried automatically is in
work. Stage notices are dropped before the graph. Durable owner events still
publish; resource waits and resumptions never do.

Redis keeps the last told key state per chat/story, written after publication
under the consumer's per-chat lock. Records have no expiry: time passing cannot
make an unchanged stop news again. Terminal notices and terminal reminders delete
them; the durable owner seam alone tells endings. Previous fingerprint records
are read as key states and retained without their old expiry.

An unreadable API or Redis result suppresses reminders: it proves no untold key
change. Durable events still publish when the gate cannot read the current story.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Protocol

import structlog

from shared.contracts.dto.story import STAGE_NOTICE_TERMINAL_STATUSES, StoryDTO, StoryStatus
from shared.contracts.dto.story_failure import STORY_FAILURE_REASON, StoryFailureCode
from shared.contracts.dto.story_planning import StoryPlanningState
from shared.contracts.vocab import OwnerNotificationEvent
from shared.redis import RedisStreamClient

logger = structlog.get_logger(__name__)

STORY_TOLD_KEY_PREFIX = "po:story_told:"
STORY_END_EVENTS = frozenset(
    {OwnerNotificationEvent.STORY_COMPLETED, OwnerNotificationEvent.STORY_FAILED}
)
INTERMEDIATE_EVENTS = frozenset(
    {
        OwnerNotificationEvent.TASK_WAITING_RESOURCES,
        OwnerNotificationEvent.TASK_WAITING_INFRASTRUCTURE,
        OwnerNotificationEvent.TASK_RESOURCES_RESUMED,
    }
)


class StoryReader(Protocol):
    async def get_story(self, story_id: str) -> StoryDTO: ...


class StoryKeyState(StrEnum):
    IN_WORK = "in_work"
    NEEDS_USER = "needs_user"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


def _key_state(
    status: StoryStatus, planning_state: str | None, failure_code: str | None
) -> StoryKeyState:
    state = {
        StoryStatus.CREATED: StoryKeyState.IN_WORK,
        StoryStatus.IN_PROGRESS: StoryKeyState.IN_WORK,
        StoryStatus.REOPENED: StoryKeyState.IN_WORK,
        StoryStatus.PR_REVIEW: StoryKeyState.IN_WORK,
        StoryStatus.DEPLOYING: StoryKeyState.IN_WORK,
        StoryStatus.TESTING: StoryKeyState.IN_WORK,
        StoryStatus.WAITING_USER_SECRET: StoryKeyState.NEEDS_USER,
        StoryStatus.WAITING_HUMAN_REVIEW: StoryKeyState.STOPPED,
        StoryStatus.COMPLETED: StoryKeyState.COMPLETED,
        StoryStatus.FAILED: StoryKeyState.FAILED,
        # Archived stories are terminal and suppressed before this state is used.
        StoryStatus.ARCHIVED: StoryKeyState.COMPLETED,
    }[status]
    # A planning the platform is retrying on its own is still work in progress;
    # only one that parked the story, or a recorded planning stop, is a stop.
    if state == StoryKeyState.IN_WORK and (
        planning_state == StoryPlanningState.PARKED
        or failure_code == StoryFailureCode.PLANNING_FAILED
    ):
        return StoryKeyState.STOPPED
    return state


def story_key_state(story: StoryDTO) -> StoryKeyState:
    reason = story.quarantine_reason
    failure_code = (
        reason.get("code")
        if isinstance(reason, dict) and reason.get("reason") == STORY_FAILURE_REASON
        else None
    )
    return _key_state(story.status, story.planning.state if story.planning else None, failure_code)


@dataclass(frozen=True)
class ToldRecord:
    key_state: StoryKeyState

    def dumps(self) -> str:
        return json.dumps({"key_state": self.key_state})

    @classmethod
    def loads(cls, raw: str) -> ToldRecord:
        data = json.loads(raw)
        if "key_state" in data:
            return cls(StoryKeyState(data["key_state"]))
        # The deployed previous format proves this state was already told.
        old = data["fingerprint"]
        return cls(
            _key_state(StoryStatus(old["status"]), old["planning_state"], old["failure_code"])
        )


@dataclass(frozen=True)
class ProactiveDecision:
    """Whether to publish and what to remember after a successful publish."""

    send: bool
    reason: str
    story_id: str = ""
    ends_story: bool = False
    key_state: StoryKeyState | None = None


def story_told_key(telegram_chat_id: str, story_id: str) -> str:
    return f"{STORY_TOLD_KEY_PREFIX}{telegram_chat_id}:{story_id}"


def _is_gated(data: dict) -> bool:
    return data.get("type") == "reminder" or (
        data.get("type") == "system_event"
        and data.get("event") == OwnerNotificationEvent.STORY_STAGE
    )


class ProactiveStoryGate:
    def __init__(self, redis_client: RedisStreamClient, stories: StoryReader) -> None:
        self._redis = redis_client
        self._stories = stories

    async def decide(self, telegram_chat_id: str, data: dict) -> ProactiveDecision:
        story_id = data.get("story_id") or ""
        log = logger.bind(telegram_chat_id=telegram_chat_id, story_id=story_id)
        if data.get("event") in INTERMEDIATE_EVENTS:
            return self._suppressed(log, story_id, "intermediate_event")
        if not story_id:
            # A reminder about no story speaks only if the user asked for it.
            if data.get("type") == "reminder" and not data.get("user_requested"):
                return self._suppressed(log, story_id, "unrequested_reminder")
            return ProactiveDecision(send=True, reason="no_story")
        if data.get("event") in STORY_END_EVENTS:
            return ProactiveDecision(
                send=True, reason="story_end_notice", story_id=story_id, ends_story=True
            )
        gated = _is_gated(data)
        try:
            story = await self._stories.get_story(story_id)
            key_state = story_key_state(story)
            if story.status in STAGE_NOTICE_TERMINAL_STATUSES:
                if gated:
                    await self._forget(telegram_chat_id, story_id)
                    return self._suppressed(log, story_id, "story_ended", key_state)
                return ProactiveDecision(
                    send=True, reason="story_end_notice", story_id=story_id, ends_story=True
                )
            if not gated:
                return ProactiveDecision(True, "ungated", story_id, key_state=key_state)
            if key_state == StoryKeyState.IN_WORK:
                return self._suppressed(log, story_id, "in_work", key_state)
            previous = await self._read_record(telegram_chat_id, story_id)
            if previous is not None and previous.key_state == key_state:
                return self._suppressed(log, story_id, "unchanged", key_state)
            return ProactiveDecision(True, "key_changed", story_id, key_state=key_state)
        except Exception as exc:
            log.warning("po_proactive_gate_unavailable", gated=gated, error=str(exc))
            if gated:
                return self._suppressed(log, story_id, "gate_unavailable")
            return ProactiveDecision(send=True, reason="gate_unavailable", story_id=story_id)

    async def record_told(self, telegram_chat_id: str, decision: ProactiveDecision) -> None:
        if not decision.story_id:
            return
        if decision.ends_story:
            await self._forget(telegram_chat_id, decision.story_id)
        elif decision.key_state is not None:
            await self._redis.redis.set(
                story_told_key(telegram_chat_id, decision.story_id),
                ToldRecord(decision.key_state).dumps(),
            )

    async def _read_record(self, telegram_chat_id: str, story_id: str) -> ToldRecord | None:
        key = story_told_key(telegram_chat_id, story_id)
        raw = await self._redis.redis.get(key)
        if raw is None:
            return None
        record = ToldRecord.loads(raw)
        # Preserve the fact already told, including legacy records with a TTL.
        # Persisting it does not mark any new state as told.
        await self._redis.redis.persist(key)
        return record

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
        log, story_id: str, reason: str, key_state: StoryKeyState | None = None
    ) -> ProactiveDecision:
        log.info("po_proactive_suppressed", reason=reason, key_state=key_state)
        return ProactiveDecision(send=False, reason=reason, story_id=story_id)
