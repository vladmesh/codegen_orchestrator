from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, Field


class QueueMeta(BaseModel):
    """Metadata for all queue messages."""

    version: Literal["1"] = "1"
    correlation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class BaseMessage(QueueMeta):
    """Base class for queue messages."""

    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    callback_stream: str | None = None


class BaseResult(BaseModel):
    """Base result for async operations."""

    request_id: str
    status: Literal["success", "failed", "error", "timeout"]
    error: str | None = None
    duration_ms: int | None = None
