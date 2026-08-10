from datetime import UTC, datetime
from typing import Literal
import uuid

from pydantic import BaseModel, Field

from shared.contracts.recipient import RejectsLegacyRecipientField
from shared.contracts.vocab import ResultStatus


class QueueMeta(RejectsLegacyRecipientField):
    """Metadata for all queue messages.

    Inherits the rejection of the removed ``user_id`` field: no queue message
    carries a recipient under that name any more, and one that arrives with it
    is refused rather than accepted with its recipient dropped.
    """

    version: Literal["1"] = "1"
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BaseMessage(QueueMeta):
    """Base class for queue messages."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    callback_stream: str | None = None


class BaseResult(BaseModel):
    """Base result for async operations."""

    request_id: str
    status: ResultStatus
    error: str | None = None
    duration_ms: int | None = None
