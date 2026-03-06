"""Contracts for PO Redis streams (po:input, po:response, po:proactive).

PO messages use flat Redis fields (not JSON 'data' wrapper), so they do NOT
inherit from BaseMessage/QueueMeta. Instead they are standalone Pydantic models
with helpers for flat-field serialization.
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
    user_id: str
    request_id: str
    user_name: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"))


class POSystemEvent(BaseModel):
    """System event from workers (progress, completed, failed, etc.)."""

    type: Literal["system_event"] = "system_event"
    event: str
    text: str
    task_id: str = ""
    user_id: str = ""
    project_id: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class POReminderMessage(BaseModel):
    """Reminder fired from the sorted set poller."""

    type: Literal["reminder"] = "reminder"
    text: str
    user_id: str
    timestamp: str = ""


POInputMessage = Annotated[
    POUserMessage | POSystemEvent | POReminderMessage,
    Field(discriminator="type"),
]


# --- PO Output messages ---


class POResponse(BaseModel):
    """Synchronous PO response (po:response:{request_id})."""

    text: str
    user_id: str
    error: str | None = None


class POProactiveMessage(BaseModel):
    """Proactive PO notification (po:proactive)."""

    text: str
    user_id: str


# --- Flat-field helpers ---


def to_flat_fields(model: BaseModel) -> dict[str, str]:
    """Convert a Pydantic model to flat string key-value pairs for XADD."""
    data = model.model_dump(mode="json")
    return {k: str(v) for k, v in data.items() if v is not None and v != ""}


def from_flat_fields(fields: dict[str, str], model_type: type[BaseModel]) -> BaseModel:
    """Parse flat Redis stream fields into a Pydantic model."""
    return model_type.model_validate(fields)
