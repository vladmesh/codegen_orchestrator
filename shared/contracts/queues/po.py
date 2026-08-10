"""Contracts for PO Redis streams (po:input, po:response, po:proactive).

PO messages use flat Redis fields (not JSON 'data' wrapper), so they do NOT
inherit from BaseMessage/QueueMeta. Instead they are standalone Pydantic models
with helpers for flat-field serialization.

Addressing: ``telegram_chat_id`` is the Telegram chat the message is delivered
to — never the internal ``User.id``. Producers that only know the internal id
(scheduler, workers) resolve it to a Telegram chat id *before* publishing;
``owner_user_id`` carries the internal id alongside it for identification in
logs and admin alerts, and is never used as a destination.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

# --- PO Input messages (po:input) ---


class POUserMessage(BaseModel):
    """User message from Telegram bot."""

    type: Literal["user_message"] = "user_message"
    text: str
    telegram_chat_id: str
    request_id: str
    user_name: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"))


class POSystemEvent(BaseModel):
    """System event from workers (progress, completed, failed, etc.).

    ``telegram_chat_id`` is empty only for events that are not addressed to a
    user at all; PO refuses to route a user-facing event without one.
    """

    type: Literal["system_event"] = "system_event"
    event: str
    text: str
    task_id: str = ""
    telegram_chat_id: str = ""
    owner_user_id: str = ""
    story_id: str = ""
    project_id: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class POReminderMessage(BaseModel):
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


class POResponse(BaseModel):
    """Synchronous PO response (po:response:{request_id})."""

    text: str
    telegram_chat_id: str
    error: str | None = None


class POProactiveMessage(BaseModel):
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
    """Convert a Pydantic model to flat string key-value pairs for XADD."""
    data = model.model_dump(mode="json")
    return {k: str(v) for k, v in data.items() if v is not None and v != ""}


def from_flat_fields(fields: dict[str, str], model_type: type[BaseModel]) -> BaseModel:
    """Parse flat Redis stream fields into a Pydantic model."""
    return model_type.model_validate(fields)
