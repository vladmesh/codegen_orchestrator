from datetime import UTC, datetime
from typing import Literal
import uuid

from pydantic import BaseModel, Field

from shared.contracts.vocab import ResultStatus


class QueueMeta(BaseModel):
    """Metadata for all queue messages."""

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
