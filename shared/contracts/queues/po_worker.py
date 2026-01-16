from datetime import datetime
import uuid

from pydantic import BaseModel, Field


class POWorkerInput(BaseModel):
    """Message from Telegram user to PO Worker."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: int  # Telegram user ID
    prompt: str  # User's message text
    callback_stream: str | None = None  # Redis stream for progress events
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class POWorkerOutput(BaseModel):
    """Response from PO Worker to Telegram user."""

    request_id: str  # Matches input request_id
    user_id: int  # Telegram user ID (for routing)
    text: str  # PO's response text
    is_final: bool = True  # False if streaming (post-MVP)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
