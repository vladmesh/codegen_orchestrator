"""Contracts for PO Redis streams (po:input, po:response, po:proactive).

PO messages use flat Redis fields (not JSON 'data' wrapper), so they do NOT
inherit from BaseMessage/QueueMeta. Instead they are standalone Pydantic models
with helpers for flat-field serialization.

Addressing: ``telegram_chat_id`` is the Telegram chat the message is delivered
to — never the internal ``User.id``. Producers that only know the internal id
(scheduler, workers) resolve it to a Telegram chat id *before* publishing;
``owner_user_id`` carries the internal id alongside it for identification in
logs and admin alerts, and is never used as a destination. A message that still
addresses a user through the removed ``user_id`` field is rejected — see
``shared.contracts.recipient``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from shared.contracts.dto.qa_verification import QAVerificationFacts
from shared.contracts.dto.story import (
    STAGE_NOTICE_STATUSES,
    WAITING_ON_BY_STATUS,
    StoryStageNoticeKind,
    StoryStatus,
    StoryWaitEstimate,
    StoryWaitingOn,
)
from shared.contracts.recipient import RejectsLegacyRecipientField
from shared.contracts.vocab import OwnerNotificationEvent, POSystemEventName

# --- PO Input messages (po:input) ---


class POUserMessage(RejectsLegacyRecipientField):
    """User message from Telegram bot."""

    type: Literal["user_message"] = "user_message"
    text: str
    telegram_chat_id: str
    request_id: str
    user_name: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"))


class POSystemEvent(RejectsLegacyRecipientField):
    """System event from workers (progress, completed, failed, etc.).

    ``telegram_chat_id`` is empty only for events that are not addressed to a
    user at all; PO refuses to route a user-facing event without one.
    """

    type: Literal["system_event"] = "system_event"
    event: POSystemEventName
    text: str
    task_id: str = ""
    telegram_chat_id: str = ""
    owner_user_id: str = ""
    story_id: str = ""
    project_id: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    #: A ``story_stage`` notice's typed facts, and only that event's: the stage
    #: the story is in, what that stage waits for, the magnitude of the wait and
    #: why the notice was sent. Carried as fields rather than left in ``text`` so
    #: what was announced can be read off the stream without parsing words.
    stage: StoryStatus | None = None
    waiting_on: StoryWaitingOn | None = None
    wait_estimate: StoryWaitEstimate | None = None
    stage_notice: StoryStageNoticeKind | None = None
    #: What the QA run that settled the story checked and could not: the names
    #: of the checks that passed and every unverified check with its reason.
    #: Carried as structured facts, JSON-encoded in its one flat field, so PO
    #: can tell the user honestly without parsing words. Set only on the event
    #: that settles a story on a QA verdict.
    qa_verification: QAVerificationFacts | None = None

    @field_validator("qa_verification", mode="before")
    @classmethod
    def _decode_flat_qa_verification(cls, value: object) -> object:
        # A flat stream field is a string; `to_flat_fields` wrote it as JSON.
        return json.loads(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _stage_fields_belong_to_stage_notices(self) -> POSystemEvent:
        stage_fields = (self.stage, self.waiting_on, self.wait_estimate, self.stage_notice)
        if self.event is not OwnerNotificationEvent.STORY_STAGE:
            if any(field is not None for field in stage_fields):
                raise ValueError(f"stage fields are carried by story_stage only, not {self.event}")
            return self
        if any(field is None for field in stage_fields):
            raise ValueError(
                "story_stage carries stage, waiting_on, wait_estimate and stage_notice"
            )
        if self.stage not in STAGE_NOTICE_STATUSES:
            raise ValueError(f"{self.stage} is not a stage a story is in work in")
        if self.waiting_on is not WAITING_ON_BY_STATUS[self.stage]:
            raise ValueError(
                f"{self.stage} waits on {WAITING_ON_BY_STATUS[self.stage]}, not {self.waiting_on}"
            )
        if not self.story_id:
            raise ValueError("story_stage names its story")
        return self


class POReminderMessage(RejectsLegacyRecipientField):
    """Reminder fired from the sorted set poller."""

    type: Literal["reminder"] = "reminder"
    text: str
    telegram_chat_id: str
    story_id: str = ""
    timestamp: str = ""


POInputMessage = Annotated[
    POUserMessage | POSystemEvent | POReminderMessage,
    Field(discriminator="type"),
]


# --- PO Output messages ---

# Where a text PO sends the user (``POResponse.text``, ``POProactiveMessage.text``)
# must start a new Telegram message. The bot splits on it before anything else
# and never shows it. ASCII RS (record separator): a control character, so it
# cannot occur in prose and a model does not emit it by accident; only code puts
# it into a text.
MESSAGE_BREAK = "\x1e"


class POResponse(RejectsLegacyRecipientField):
    """Synchronous PO response (po:response:{request_id})."""

    text: str
    telegram_chat_id: str
    error: str | None = None


class POProactiveMessage(RejectsLegacyRecipientField):
    """Proactive PO notification (po:proactive).

    Carries the identifiers the transport needs to raise a useful admin alert
    when delivery to ``telegram_chat_id`` cannot be completed.
    """

    text: str
    telegram_chat_id: str
    owner_user_id: str = ""
    event: str = ""
    story_id: str = ""
    project_id: str = ""


# --- Addressing helpers ---


def po_thread_id(telegram_chat_id: str) -> str:
    """The PO conversation a message belongs to.

    Keyed by the Telegram chat, so a message the user typed and an event the
    pipeline raised about their project land in the same thread no matter which
    producer emitted it.
    """
    return f"po-chat-{telegram_chat_id}"


def proactive_from_input(source: dict, text: str, telegram_chat_id: str) -> POProactiveMessage:
    """Build the proactive notification PO sends back for an input message.

    The recipient is passed in — it is the key the consumer already routed and
    locked on — and the identifiers travel from the incoming message, so the
    transport can name the story, project and event if delivery fails.
    """
    return POProactiveMessage(
        text=text,
        telegram_chat_id=telegram_chat_id,
        owner_user_id=source.get("owner_user_id", ""),
        event=source.get("event", ""),
        story_id=source.get("story_id", ""),
        project_id=source.get("project_id", ""),
    )


# --- Flat-field helpers ---


def to_flat_fields(model: BaseModel) -> dict[str, str]:
    """Convert a Pydantic model to flat string key-value pairs for XADD.

    A structured field (an object or a list) is written as JSON, the one form
    its model decodes back.
    """
    data = model.model_dump(mode="json")
    return {
        k: json.dumps(v, separators=(",", ":")) if isinstance(v, dict | list) else str(v)
        for k, v in data.items()
        if v is not None and v != ""
    }


def from_flat_fields(fields: dict[str, str], model_type: type[BaseModel]) -> BaseModel:
    """Parse flat Redis stream fields into a Pydantic model."""
    return model_type.model_validate(fields)
