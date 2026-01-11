from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict


class IncidentSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class IncidentDTO(BaseModel):
    """Incident response."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    severity: IncidentSeverity
    status: IncidentStatus
    title: str
    description: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
